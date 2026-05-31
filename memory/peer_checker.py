"""Intra-session peer cross-check (Tier 1 drift detection).

The Conductor calls `PeerChecker` after parsing each agent's staging output, before
anything touches the repo. It operates on the *raw* agent-declared schemas in the
VDB ``live/`` directory — which carry full ``exposes``/``consumes`` objects with
signatures — so it can detect signature-level mismatches the flattened
``ContractSummary`` cannot.

Conflict types:
    COLLISION          — same symbol exposed by two agents
    INTERFACE_MISMATCH — agent consumes a symbol with the wrong signature, or one
                         no peer exposes
    ASSUMPTION_CLASH   — contradicting assumptions about shared state
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from pathlib import Path

    from memory.contract_store import VectorContractStore

ConflictKind = Literal["COLLISION", "INTERFACE_MISMATCH", "ASSUMPTION_CLASH"]


@dataclass
class Conflict:
    kind: ConflictKind
    agent_id: str            # agent with the problem
    peer_id: str             # peer it conflicts with
    symbol: str              # symbol in question
    agent_signature: str     # what the agent declared
    peer_signature: str      # what the peer declared
    detail: str              # human-readable explanation
    fix: str                 # exact fix instruction


@dataclass
class _AgentView:
    """Normalized view of one agent's declared schema across its modules."""

    agent_id: str
    exposes: dict[str, str] = field(default_factory=dict)        # symbol -> signature
    consumes: list[dict[str, str]] = field(default_factory=list)  # {symbol, expected_signature, consumed_in}
    assumptions: list[str] = field(default_factory=list)
    subsystem: str = ""


class PeerChecker:
    """Cross-checks an agent's contracts against peer agents' live schemas."""

    def __init__(self, store: VectorContractStore) -> None:
        self.store = store

    # ── Live schema I/O ──────────────────────────────────────────

    def read_all_live(self) -> dict[str, _AgentView]:
        """Read all live schema files, grouped into one _AgentView per agent."""
        views: dict[str, _AgentView] = {}
        live_dir: Path = self.store.live_dir
        if not live_dir.exists():
            return views
        for path in sorted(live_dir.glob("*.json")):
            agent_id, _, module = path.stem.rpartition("__")
            if not agent_id:
                agent_id, module = path.stem, path.stem
            try:
                schema = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            view = views.setdefault(agent_id, _AgentView(agent_id=agent_id, subsystem=module))
            _merge_schema_into(view, schema)
        return views

    # ── Cross-check ──────────────────────────────────────────────

    def check(self, agent_id: str, contracts: dict[str, Any]) -> list[Conflict]:
        """Check this agent's declared contracts against all peers already in live/."""
        peers = {aid: v for aid, v in self.read_all_live().items() if aid != agent_id}

        mine = _AgentView(agent_id=agent_id)
        _merge_schema_into(mine, contracts)

        conflicts: list[Conflict] = []
        conflicts.extend(self._check_collisions(mine, peers))
        conflicts.extend(self._check_interface_mismatches(mine, peers))
        conflicts.extend(self._check_assumption_clashes(mine, peers))
        return conflicts

    def _check_collisions(self, mine: _AgentView, peers: dict[str, _AgentView]) -> list[Conflict]:
        out: list[Conflict] = []
        for symbol, sig in mine.exposes.items():
            for peer in peers.values():
                if symbol in peer.exposes:
                    out.append(
                        Conflict(
                            kind="COLLISION",
                            agent_id=mine.agent_id,
                            peer_id=peer.agent_id,
                            symbol=symbol,
                            agent_signature=sig,
                            peer_signature=peer.exposes[symbol],
                            detail=f"Symbol '{symbol}' is exposed by both {mine.agent_id} and {peer.agent_id}.",
                            fix=f"Rename or namespace '{symbol}' so exactly one agent owns it.",
                        )
                    )
        return out

    def _check_interface_mismatches(self, mine: _AgentView, peers: dict[str, _AgentView]) -> list[Conflict]:
        out: list[Conflict] = []
        for consumed in mine.consumes:
            symbol = consumed.get("symbol", "")
            expected = _norm_sig(consumed.get("expected_signature", ""))
            provider = next((p for p in peers.values() if symbol in p.exposes), None)
            if provider is None:
                out.append(
                    Conflict(
                        kind="INTERFACE_MISMATCH",
                        agent_id=mine.agent_id,
                        peer_id="(none)",
                        symbol=symbol,
                        agent_signature=consumed.get("expected_signature", ""),
                        peer_signature="(not exposed by any peer)",
                        detail=f"Consumes '{symbol}' but no peer exposes it.",
                        fix=f"Ensure an agent exposes '{symbol}', or correct the consumes declaration.",
                    )
                )
                continue
            actual = _norm_sig(provider.exposes[symbol])
            if expected and actual and expected != actual:
                out.append(
                    Conflict(
                        kind="INTERFACE_MISMATCH",
                        agent_id=mine.agent_id,
                        peer_id=provider.agent_id,
                        symbol=symbol,
                        agent_signature=consumed.get("expected_signature", ""),
                        peer_signature=provider.exposes[symbol],
                        detail=(
                            f"Signature mismatch on '{symbol}': you expect "
                            f"'{consumed.get('expected_signature', '')}', "
                            f"{provider.agent_id} exposes '{provider.exposes[symbol]}'."
                        ),
                        fix=f"Align your consumes signature to '{provider.exposes[symbol]}'.",
                    )
                )
        return out

    def _check_assumption_clashes(self, mine: _AgentView, peers: dict[str, _AgentView]) -> list[Conflict]:
        out: list[Conflict] = []
        for peer in peers.values():
            for a in mine.assumptions:
                for b in peer.assumptions:
                    if _assumptions_contradict(a, b):
                        out.append(
                            Conflict(
                                kind="ASSUMPTION_CLASH",
                                agent_id=mine.agent_id,
                                peer_id=peer.agent_id,
                                symbol=a,
                                agent_signature=a,
                                peer_signature=b,
                                detail=f"Assumption '{a}' contradicts {peer.agent_id}'s '{b}'.",
                                fix=f"Reconcile shared-state assumption: '{a}' vs '{b}'.",
                            )
                        )
        return out

    # ── Reporting ────────────────────────────────────────────────

    def format_conflict_report(
        self,
        agent_id: str,
        subsystem: str,
        conflicts: list[Conflict],
        attempt: int,
        staging_dir: Path,
    ) -> str:
        """Format a conflict report; specificity escalates with attempt number.

        Attempt 1: what's wrong and why.
        Attempt 2: + the exact correct signature.
        Attempt 3+: + an exact code block.
        """
        lines = [
            f"CONDUCTOR — CONFLICT REPORT (Attempt {attempt})",
            f"Agent {agent_id} | Subsystem: {subsystem}",
            f"Timestamp: {datetime.now(UTC).isoformat()}",
            "",
        ]
        for c in conflicts:
            lines.append("CONFLICT DETECTED:")
            lines.append(f"  Type: {c.kind}")
            lines.append(f"  Symbol: {c.symbol}")
            lines.append(f"  {c.detail}")
            lines.append("")
            lines.append("FIX REQUIRED:")
            lines.append(f"  {c.fix}")
            if attempt >= 2 and c.peer_signature and c.peer_signature != "(not exposed by any peer)":
                lines.append(f"  Correct signature: {c.peer_signature}")
            if attempt >= 3 and c.peer_signature and c.peer_signature != "(not exposed by any peer)":
                lines.append("  Exact code:")
                lines.append(f"    {c.peer_signature} {{")
                lines.append("        // implement to match the exposed contract")
                lines.append("    }")
            lines.append("")
        lines.append("Rewrite affected file(s) and resubmit to:")
        lines.append(f"  {staging_dir}")
        lines.append("")
        lines.append("Write .done when complete.")
        return "\n".join(lines)


# ── Schema normalization helpers ─────────────────────────────────


def _iter_module_entries(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Yield per-module entry dicts from any accepted schema shape."""
    if "modules" in schema and isinstance(schema["modules"], dict):
        return [e for e in schema["modules"].values() if isinstance(e, dict)]
    # A bare {module: entry} mapping, or a single module entry.
    if any(k in schema for k in ("exposes", "consumes", "assumptions", "contracts")):
        return [schema]
    return [e for e in schema.values() if isinstance(e, dict)]


def _merge_schema_into(view: _AgentView, schema: dict[str, Any]) -> None:
    for entry in _iter_module_entries(schema):
        for exp in entry.get("exposes", []):
            if isinstance(exp, dict) and exp.get("symbol"):
                view.exposes[str(exp["symbol"])] = str(exp.get("signature", ""))
            elif isinstance(exp, str):
                view.exposes.setdefault(exp, "")
        for con in entry.get("consumes", []):
            if isinstance(con, dict) and con.get("symbol"):
                view.consumes.append(
                    {
                        "symbol": str(con["symbol"]),
                        "expected_signature": str(con.get("expected_signature", "")),
                        "consumed_in": str(con.get("consumed_in", "")),
                    }
                )
        for a in entry.get("assumptions", []):
            if isinstance(a, str):
                view.assumptions.append(a)


def _norm_sig(sig: str) -> str:
    """Normalize a signature for comparison — collapse whitespace."""
    return " ".join(sig.split())


_NEGATORS = ("no_", "no ", "not ", "non-", "!", "without ")


def _assumptions_contradict(a: str, b: str) -> bool:
    """Detect contradicting assumptions about shared state."""
    a_l, b_l = a.strip().lower(), b.strip().lower()
    if a_l == b_l:
        return False
    # std vs no_std family
    if {a_l, b_l} == {"std", "no_std"} or {a_l, b_l} == {"std", "no std"}:
        return True
    # one is a negation of the other ("X" vs "no X" / "not X" / "!X")
    for neg in _NEGATORS:
        if a_l == f"{neg}{b_l}" or b_l == f"{neg}{a_l}":
            return True
        if a_l == f"{neg}{b_l}".replace(" ", "_") or b_l == f"{neg}{a_l}".replace(" ", "_"):
            return True
    return False
