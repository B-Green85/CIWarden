"""Proofing — dependency-ordered parse + intra-session peer check, before commit.

The Conductor parses each agent's staging output in dependency order, writes its
declared schema to the VDB ``live/`` directory (rich exposes/consumes), and runs the
``PeerChecker`` against peers already parsed. On conflict it writes an escalating
report to the agent's staging dir and waits for a fresh ``.done``. No retry limit.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from conductor import distributor
from conductor.vdb_io import store_for
from memory.peer_checker import PeerChecker

if TYPE_CHECKING:
    from conductor.session import AgentSpec, ConductorSession
    from memory.contract_store import VectorContractStore
    from memory.peer_checker import Conflict


def build_dependency_graph(session: ConductorSession) -> dict[str, list[str]]:
    """Map each agent's module_key to the module_keys it depends on."""
    return {agent.module_key: list(agent.dependencies) for agent in session.agents}


def parse_order(graph: dict[str, list[str]]) -> list[str]:
    """Topological order — no-dependency modules first. Stable; cycles broken arbitrarily."""
    ordered: list[str] = []
    satisfied: set[str] = set()
    remaining = dict(graph)
    while remaining:
        ready = [m for m, deps in remaining.items() if all(d in satisfied or d not in graph for d in deps)]
        if not ready:
            # Dependency cycle — emit the rest in declaration order to make progress.
            ready = list(remaining)
        for module in sorted(ready):
            ordered.append(module)
            satisfied.add(module)
            remaining.pop(module, None)
    return ordered


def agents_in_parse_order(session: ConductorSession) -> list[AgentSpec]:
    order = parse_order(build_dependency_graph(session))
    by_key = {a.module_key: a for a in session.agents}
    return [by_key[k] for k in order if k in by_key]


def build_expected_interfaces(session: ConductorSession) -> dict[str, dict[str, list[str]]]:
    """Extract what each agent should deliver, keyed by module_key.

    Heuristic: parse ``exposes:`` / ``deliverables:`` lines from each prompt. Agents
    that deliver less than promised are flagged in addition to peer conflicts.
    """
    expected: dict[str, dict[str, list[str]]] = {}
    for agent in session.agents:
        deliverables: list[str] = []
        for line in agent.prompt.splitlines():
            stripped = line.strip().lower()
            if stripped.startswith(("exposes:", "deliverables:", "- exposes", "* exposes")):
                _, _, rest = line.partition(":")
                deliverables.extend(item.strip() for item in rest.split(",") if item.strip())
        expected[agent.module_key] = {"deliverables": deliverables}
    return expected


def initialize_vdb_metadata(session: ConductorSession) -> None:
    """Write dependency_graph.json and expected_interfaces.json to the VDB."""
    store = store_for(session)
    store.write_dependency_graph(build_dependency_graph(session))
    store.write_expected_interfaces(build_expected_interfaces(session))


# ── Live-schema staging + single-agent proof ─────────────────────


def _publish_live(store: VectorContractStore, agent: AgentSpec, schema: dict[str, Any]) -> None:
    """Write the agent's rich per-module schema to live/{agent_id}__{module}.json."""
    modules = schema.get("modules") if isinstance(schema, dict) else None
    if not isinstance(modules, dict):
        return
    store.live_dir.mkdir(parents=True, exist_ok=True)
    for module, entry in modules.items():
        path = store.live_dir / f"{agent.agent_id}__{module}.json"
        path.write_text(json.dumps(entry, indent=2))


def proof_agent(session: ConductorSession, store: VectorContractStore, agent: AgentSpec) -> list[Conflict]:
    """Parse one agent's staging output, publish to live/, and peer-check it.

    Returns the list of Conflict objects (empty when clean).
    """
    schema = distributor.read_agent_schema(session, agent)
    _publish_live(store, agent, schema)
    return PeerChecker(store).check(agent.agent_id, schema)


def proof_session(
    session: ConductorSession,
    *,
    poll_interval: float = 1.0,
    timeout: float | None = None,
) -> bool:
    """Proof all agents in dependency order, escalating conflict reports until clean.

    Returns True when every agent is clean. Blocks waiting for agent ``.done`` signals
    between retries (no retry limit — the agent works until it's right).
    """
    store = store_for(session)
    checker = PeerChecker(store)
    attempts: dict[str, int] = {a.agent_id: 1 for a in session.agents}

    for agent in agents_in_parse_order(session):
        while True:
            schema = distributor.read_agent_schema(session, agent)
            _publish_live(store, agent, schema)
            conflicts = checker.check(agent.agent_id, schema)
            if not conflicts:
                break
            report = checker.format_conflict_report(
                agent.agent_id,
                agent.subsystem_path,
                conflicts,
                attempts[agent.agent_id],
                distributor.staging_dir(session, agent),
            )
            distributor.write_conflict_report(session, agent, report)
            attempts[agent.agent_id] += 1
            if not distributor.wait_for_done(session, [agent], poll_interval=poll_interval, timeout=timeout):
                return False
    return True
