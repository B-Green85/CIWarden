"""Core Generational Memory Gate — extract contracts, score drift, pass or block."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from gates.base_gate import GateResult, GateStatus
from memory.contract_store import BaseContractStore  # noqa: TCH001
from memory.drift_scorer import DRIFT_THRESHOLD, score_drift
from memory.llm_client import LLMClient  # noqa: TCH001
from memory.models import Checkpoint, ContractSummary, DriftResult

_EXCLUDED_DIRS = {"tests", "__pycache__", ".venv", ".cdmad", ".git", "node_modules"}


def _discover_source_files(root: str = ".") -> dict[str, str]:
    """Discover Python source files grouped by module.

    Returns a dict mapping module names to concatenated source code.
    """
    root_path = Path(root).resolve()
    modules: dict[str, list[Path]] = {}

    for py_file in sorted(root_path.rglob("*.py")):
        rel = py_file.relative_to(root_path)
        parts = rel.parts

        if any(part in _EXCLUDED_DIRS for part in parts):
            continue
        if rel.name.startswith("."):
            continue

        module_name = parts[0].removesuffix(".py") if len(parts) == 1 else parts[0]
        modules.setdefault(module_name, []).append(py_file)

    result: dict[str, str] = {}
    for module_name, files in modules.items():
        chunks: list[str] = []
        for f in files:
            chunks.append(f"# --- {f.relative_to(root_path)} ---\n{f.read_text()}")
        result[module_name] = "\n\n".join(chunks)

    return result


def _compute_source_sha(source_files: dict[str, str]) -> str:
    """Compute a deterministic SHA256 over sorted source file contents."""
    h = hashlib.sha256()
    for key in sorted(source_files):
        h.update(key.encode())
        h.update(source_files[key].encode())
    return h.hexdigest()


_CACHE_FILENAME = "memory_cache.json"


class MemoryGate:
    def __init__(
        self,
        llm_client: LLMClient,
        store: BaseContractStore,
        drift_threshold: float = DRIFT_THRESHOLD,
        repo_name: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        self.llm = llm_client
        self.store = store
        self.repo_name = repo_name
        self.agent_id = agent_id
        env_override = os.environ.get("CDMAD_DRIFT_THRESHOLD")
        self.threshold = float(env_override) if env_override else drift_threshold

    @property
    def _managed(self) -> bool:
        """Conductor-managed run — the VDB is already committed; gate is read-only."""
        return os.environ.get("CDMAD_MANAGED") == "1"

    @property
    def _is_vdb_store(self) -> bool:
        """True when the store buffers writes until an atomic commit_session."""
        return hasattr(self.store, "commit_session")

    async def run(self, source_files: dict[str, str] | None = None) -> GateResult:
        start = time.time()

        try:
            return await self._execute(source_files, start)
        except Exception as e:
            duration = int((time.time() - start) * 1000)
            return GateResult(
                gate="memory",
                status=GateStatus.FAIL,
                output=f"Memory gate error: {e}",
                exit_code=1,
                duration_ms=duration,
            )

    @property
    def _cache_enabled(self) -> bool:
        return os.environ.get("CDMAD_MEMORY_CACHE") == "1"

    def _cache_path(self) -> Path | None:
        """Return cache file path if store has a file-based root."""
        root = getattr(self.store, "root", None)
        if root is None:
            return None
        return Path(root) / _CACHE_FILENAME

    def _read_cached_sha(self) -> str | None:
        path = self._cache_path()
        if path is None or not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            val = data.get("source_sha")
            return str(val) if val is not None else None
        except (json.JSONDecodeError, OSError):
            return None

    def _write_cache(self, source_sha: str) -> None:
        path = self._cache_path()
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"source_sha": source_sha}))

    async def _execute(self, source_files: dict[str, str] | None, start: float) -> GateResult:
        # Managed mode (Conductor): the VDB was committed during proofing — read-only.
        if self._managed:
            return self._execute_managed(start)
        return await self._execute_solo(source_files, start)

    async def _execute_solo(self, source_files: dict[str, str] | None, start: float) -> GateResult:
        """Solo CI path: extract, score against the last committed baseline, persist."""
        if source_files is None:
            source_files = _discover_source_files()

        if not source_files:
            duration = int((time.time() - start) * 1000)
            return GateResult(
                gate="memory",
                status=GateStatus.PASS,
                output="No source files found — nothing to extract.",
                exit_code=0,
                duration_ms=duration,
            )

        # Cache check: skip LLM extraction if source files are unchanged since last pass
        source_sha = _compute_source_sha(source_files)
        if self._cache_enabled:
            cached_sha = self._read_cached_sha()
            if cached_sha == source_sha:
                duration = int((time.time() - start) * 1000)
                return GateResult(
                    gate="memory",
                    status=GateStatus.PASS,
                    output=f"Memory Gate [CACHED] — source unchanged (sha={source_sha[:12]})",
                    exit_code=0,
                    duration_ms=duration,
                )

        is_vdb = self._is_vdb_store
        session = self.store.get_or_create_session()
        session = self.store.advance_session(session)

        gen_id = session.current_generation_id
        seq = session.current_sequence

        all_summaries: list[ContractSummary] = []
        all_drifts: list[DriftResult] = []
        output_lines: list[str] = []
        max_drift = 0.0

        for module_name, code in sorted(source_files.items()):
            summary = await self.llm.extract_contracts(code, module_name, gen_id, seq)

            if is_vdb:
                # VDB buffers until commit_session below; the baseline is the last
                # committed session (this run is not yet in the index).
                baseline = self.store.load_latest_contract(module_name)
            else:
                # JSON store persists immediately; the baseline is the prior sequence.
                self.store.save_contract(summary)
                baseline = self.store.load_previous_contract(module_name, before_sequence=seq)
            all_summaries.append(summary)

            if baseline is None:
                output_lines.append(f"  {module_name}: first generation (drift=0.0) ✓")
                all_drifts.append(DriftResult(score=0.0, passed=True, detail="First generation."))
                continue

            drift = score_drift(baseline, summary)
            all_drifts.append(drift)
            max_drift = max(max_drift, drift.score)

            status_mark = "✓" if drift.passed else "✗"
            output_lines.append(f"  {module_name}: drift={drift.score:.4f} {status_mark}")
            if drift.detail:
                output_lines.append(f"    {drift.detail}")

        # Save checkpoint
        checkpoint = Checkpoint(
            session_id=session.session_id,
            generation_id=gen_id,
            sequence=seq,
            contracts=all_summaries,
            drift_history=all_drifts,
        )
        self.store.save_checkpoint(checkpoint)

        # Atomic commit of the whole generation (VDB only) — once, after the loop.
        if is_vdb:
            self.store.commit_session(all_summaries)  # type: ignore[attr-defined]

        duration = int((time.time() - start) * 1000)
        passed = max_drift < self.threshold

        header = "PASS" if passed else f"BLOCKED — max drift {max_drift:.4f} >= {self.threshold}"
        output = f"Memory Gate [{header}] (gen={gen_id}, seq={seq})\n" + "\n".join(output_lines)

        if passed and self._cache_enabled:
            self._write_cache(source_sha)

        return GateResult(
            gate="memory",
            status=GateStatus.PASS if passed else GateStatus.FAIL,
            output=output,
            exit_code=0 if passed else 1,
            duration_ms=duration,
        )

    def _execute_managed(self, start: float) -> GateResult:
        """Managed mode: compare committed session N vs N-1. No LLM, no writes."""
        list_modules = getattr(self.store, "list_modules", None)
        modules = sorted(list_modules()) if callable(list_modules) else []

        if not modules:
            duration = int((time.time() - start) * 1000)
            return GateResult(
                gate="memory",
                status=GateStatus.PASS,
                output="Memory Gate [MANAGED] — no committed contracts to compare.",
                exit_code=0,
                duration_ms=duration,
            )

        output_lines: list[str] = []
        max_drift = 0.0

        for module_name in modules:
            current = self.store.load_latest_contract(module_name)
            previous = self.store.load_previous_contract(module_name, before_sequence=2**31)
            if current is None:
                continue
            if previous is None:
                output_lines.append(f"  {module_name}: first generation (drift=0.0) ✓")
                continue

            drift = score_drift(previous, current)
            max_drift = max(max_drift, drift.score)
            status_mark = "✓" if drift.passed else "✗"
            output_lines.append(f"  {module_name}: drift={drift.score:.4f} {status_mark}")
            if drift.detail:
                output_lines.append(f"    {drift.detail}")

        duration = int((time.time() - start) * 1000)
        passed = max_drift < self.threshold
        header = "PASS" if passed else f"BLOCKED — max drift {max_drift:.4f} >= {self.threshold}"
        output = f"Memory Gate [MANAGED {header}]\n" + "\n".join(output_lines)

        return GateResult(
            gate="memory",
            status=GateStatus.PASS if passed else GateStatus.FAIL,
            output=output,
            exit_code=0 if passed else 1,
            duration_ms=duration,
        )
