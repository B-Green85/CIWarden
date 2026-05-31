"""Contract storage interface and implementations for the Generational Memory Gate."""

from __future__ import annotations

import json
import os
import socket
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from memory.models import Checkpoint, ContractSummary, Session

# ── Abstract interface ──────────────────────────────────────────


class BaseContractStore(ABC):
    """Abstract contract store interface.

    All storage backends must implement these methods. The memory gate
    depends only on this interface — swap the backend by injecting a
    different implementation.
    """

    # ── Contracts ────────────────────────────────────────────────

    @abstractmethod
    def save_contract(self, summary: ContractSummary) -> None:
        """Persist a contract summary."""
        ...

    @abstractmethod
    def load_latest_contract(self, module: str) -> ContractSummary | None:
        """Load the most recent contract for a module."""
        ...

    @abstractmethod
    def load_previous_contract(self, module: str, before_sequence: int) -> ContractSummary | None:
        """Load the most recent contract for a module with sequence < before_sequence."""
        ...

    @abstractmethod
    def list_contracts(self, module: str) -> list[ContractSummary]:
        """List all contracts for a module, ordered by sequence."""
        ...

    # ── Checkpoints ──────────────────────────────────────────────

    @abstractmethod
    def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        """Persist a checkpoint."""
        ...

    @abstractmethod
    def load_latest_checkpoint(self, session_id: str) -> Checkpoint | None:
        """Load the most recent checkpoint for a session."""
        ...

    # ── Session ──────────────────────────────────────────────────

    @abstractmethod
    def get_or_create_session(self) -> Session:
        """Load the active session or create a new one."""
        ...

    @abstractmethod
    def save_session(self, session: Session) -> None:
        """Persist session state."""
        ...

    @abstractmethod
    def advance_session(self, session: Session) -> Session:
        """Increment session sequence and persist."""
        ...


# ── File-based implementation ───────────────────────────────────


class ContractStore(BaseContractStore):
    """File-based contract store using JSON files in a .cdmad/ directory."""

    def __init__(self, root: str | Path = ".cdmad") -> None:
        self.root = Path(root)
        self.contracts_dir = self.root / "contracts"
        self.checkpoints_dir = self.root / "checkpoints"
        self.session_file = self.root / "session.json"

    def ensure_dirs(self) -> None:
        self.contracts_dir.mkdir(parents=True, exist_ok=True)
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)

    # ── Contracts ────────────────────────────────────────────────

    def _module_dir(self, module: str) -> Path:
        return self.contracts_dir / module

    def _contract_filename(self, sequence: int) -> str:
        return f"gen_{sequence:04d}.json"

    def save_contract(self, summary: ContractSummary) -> None:
        self.ensure_dirs()
        module_dir = self._module_dir(summary.module)
        module_dir.mkdir(parents=True, exist_ok=True)
        path = module_dir / self._contract_filename(summary.sequence)
        path.write_text(summary.model_dump_json(indent=2))

    def load_contract(self, module: str, generation_id: str) -> ContractSummary | None:
        module_dir = self._module_dir(module)
        if not module_dir.exists():
            return None
        for path in sorted(module_dir.glob("gen_*.json")):
            summary = ContractSummary.model_validate_json(path.read_text())
            if summary.generation_id == generation_id:
                return summary
        return None

    def load_latest_contract(self, module: str) -> ContractSummary | None:
        module_dir = self._module_dir(module)
        if not module_dir.exists():
            return None
        files = sorted(module_dir.glob("gen_*.json"))
        if not files:
            return None
        return ContractSummary.model_validate_json(files[-1].read_text())

    def load_previous_contract(self, module: str, before_sequence: int) -> ContractSummary | None:
        module_dir = self._module_dir(module)
        if not module_dir.exists():
            return None
        candidates: list[ContractSummary] = []
        for path in sorted(module_dir.glob("gen_*.json")):
            summary = ContractSummary.model_validate_json(path.read_text())
            if summary.sequence < before_sequence:
                candidates.append(summary)
        return candidates[-1] if candidates else None

    def list_contracts(self, module: str) -> list[ContractSummary]:
        module_dir = self._module_dir(module)
        if not module_dir.exists():
            return []
        results: list[ContractSummary] = []
        for path in sorted(module_dir.glob("gen_*.json")):
            results.append(ContractSummary.model_validate_json(path.read_text()))
        return results

    def list_modules(self) -> list[str]:
        if not self.contracts_dir.exists():
            return []
        return sorted(d.name for d in self.contracts_dir.iterdir() if d.is_dir())

    # ── Checkpoints ──────────────────────────────────────────────

    def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        self.ensure_dirs()
        filename = f"checkpoint_{checkpoint.session_id}_{checkpoint.sequence:04d}.json"
        path = self.checkpoints_dir / filename
        path.write_text(checkpoint.model_dump_json(indent=2))

    def load_latest_checkpoint(self, session_id: str) -> Checkpoint | None:
        if not self.checkpoints_dir.exists():
            return None
        pattern = f"checkpoint_{session_id}_*.json"
        files = sorted(self.checkpoints_dir.glob(pattern))
        if not files:
            return None
        return Checkpoint.model_validate_json(files[-1].read_text())

    # ── Session ──────────────────────────────────────────────────

    def load_session(self) -> Session | None:
        if not self.session_file.exists():
            return None
        return Session.model_validate_json(self.session_file.read_text())

    def save_session(self, session: Session) -> None:
        self.ensure_dirs()
        self.session_file.write_text(session.model_dump_json(indent=2))

    def get_or_create_session(self) -> Session:
        session = self.load_session()
        if session is not None:
            return session
        session_id = uuid.uuid4().hex[:12]
        gen_id = f"gen_{session_id}_001"
        session = Session(
            session_id=session_id,
            current_generation_id=gen_id,
            current_sequence=0,
        )
        self.save_session(session)
        return session

    def advance_session(self, session: Session) -> Session:
        session.current_sequence += 1
        seq = session.current_sequence
        session.current_generation_id = f"gen_{session.session_id}_{seq:03d}"
        self.save_session(session)
        return session


# ── JSON-backed VDB implementation ──────────────────────────────


class VectorContractStore(BaseContractStore):
    """Repo- and session-scoped JSON "vector" contract store.

    Layout under ``{vdb_root}/{repo_name}/``:
        index.json                  latest_session / previous_session pointers
        session_{id}.json           committed generation — full contract corpus
        session_state.json          VDB-backend session state
        expected_interfaces.json    Conductor pre-validation baseline
        dependency_graph.json       parse order + dependency map
        checkpoints/                checkpoint_{session}_{seq}.json
        live/{agent_id}__{module}.json   in-progress, pending until commit_session

    This is the JSON stand-in for a future ChromaDB/Pinecone swap; the gate logic
    depends only on BaseContractStore and never sees the difference. ``save_contract``
    writes a *pending* live schema; nothing reaches a committed session file until
    ``commit_session`` is called (by the Conductor, or once by the gate in solo mode).
    """

    def __init__(
        self,
        repo_name: str,
        vdb_root: str | Path = ".cdmad/vdb",
        session_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        self.repo_name = repo_name
        self.repo_dir = Path(vdb_root) / repo_name
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.agent_id = agent_id or os.environ.get("CDMAD_AGENT_ID") or f"{socket.gethostname()}_{os.getpid()}"
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        (self.repo_dir / "live").mkdir(exist_ok=True)

    @property
    def root(self) -> Path:
        """Alias for repo_dir — keeps MemoryGate._cache_path consistent across backends."""
        return self.repo_dir

    # ── Paths ────────────────────────────────────────────────────

    @property
    def index_file(self) -> Path:
        return self.repo_dir / "index.json"

    @property
    def live_dir(self) -> Path:
        return self.repo_dir / "live"

    @property
    def checkpoints_dir(self) -> Path:
        return self.repo_dir / "checkpoints"

    @property
    def session_state_file(self) -> Path:
        return self.repo_dir / "session_state.json"

    def _session_file(self, session_id: str) -> Path:
        return self.repo_dir / f"session_{session_id}.json"

    def _live_file(self, module: str) -> Path:
        return self.live_dir / f"{self.agent_id}__{module}.json"

    # ── Index ────────────────────────────────────────────────────

    def _load_index(self) -> dict[str, Any]:
        if not self.index_file.exists():
            return {}
        try:
            data = json.loads(self.index_file.read_text())
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _write_index(self, index: dict[str, Any]) -> None:
        self.index_file.write_text(json.dumps(index, indent=2))

    def _read_session_modules(self, session_id: str | None) -> dict[str, Any]:
        if not session_id:
            return {}
        path = self._session_file(session_id)
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
        modules = payload.get("modules")
        return modules if isinstance(modules, dict) else {}

    @staticmethod
    def _to_summary(entry: Any) -> ContractSummary | None:
        if not isinstance(entry, dict):
            return None
        return ContractSummary.model_validate(entry)

    # ── Contracts ────────────────────────────────────────────────

    def save_contract(self, summary: ContractSummary) -> None:
        """Write a *pending* live schema, keyed by (agent_id, module).

        Not committed to a session file until ``commit_session`` is called.
        """
        self.live_dir.mkdir(parents=True, exist_ok=True)
        self._live_file(summary.module).write_text(summary.model_dump_json(indent=2))

    def load_latest_contract(self, module: str) -> ContractSummary | None:
        """Most recent committed contract: index → latest_session → module."""
        index = self._load_index()
        modules = self._read_session_modules(index.get("latest_session"))
        summary = self._to_summary(modules.get(module))
        if summary is not None:
            return summary
        return self._scan_for_module(module, newest=True)

    def load_previous_contract(self, module: str, before_sequence: int) -> ContractSummary | None:
        """Prior committed contract: index → previous_session → module.

        VDB ordering is driven by the index pointers, not by ``before_sequence``;
        the parameter is retained for interface compatibility with DriftScorer.
        """
        index = self._load_index()
        modules = self._read_session_modules(index.get("previous_session"))
        summary = self._to_summary(modules.get(module))
        if summary is not None:
            return summary
        return self._scan_for_module(module, newest=False, before_sequence=before_sequence)

    def _scan_for_module(
        self,
        module: str,
        *,
        newest: bool,
        before_sequence: int | None = None,
    ) -> ContractSummary | None:
        """Fallback when the index is stale: scan session files by sequence."""
        candidates: list[tuple[int, ContractSummary]] = []
        for path in self.repo_dir.glob("session_*.json"):
            try:
                payload = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            modules = payload.get("modules", {})
            entry = modules.get(module) if isinstance(modules, dict) else None
            summary = self._to_summary(entry)
            if summary is None:
                continue
            seq = int(payload.get("sequence", summary.sequence))
            if before_sequence is not None and seq >= before_sequence:
                continue
            candidates.append((seq, summary))
        if not candidates:
            return None
        candidates.sort(key=lambda t: t[0])
        # newest=True → highest sequence overall; newest=False → highest below
        # before_sequence (candidates already filtered). Either way: the last one.
        return candidates[-1][1]

    def list_contracts(self, module: str) -> list[ContractSummary]:
        """All contracts for a module across committed sessions, ordered by sequence."""
        results: list[tuple[int, ContractSummary]] = []
        for path in sorted(self.repo_dir.glob("session_*.json")):
            try:
                payload = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            modules = payload.get("modules", {})
            entry = modules.get(module) if isinstance(modules, dict) else None
            summary = self._to_summary(entry)
            if summary is None:
                continue
            seq = int(payload.get("sequence", summary.sequence))
            results.append((seq, summary))
        results.sort(key=lambda t: t[0])
        return [s for _, s in results]

    def list_modules(self) -> list[str]:
        """Module names present in the latest committed session."""
        index = self._load_index()
        return sorted(self._read_session_modules(index.get("latest_session")).keys())

    # ── New methods (Conductor / atomic commit) ──────────────────

    def commit_session(self, contracts: list[ContractSummary] | dict[str, Any]) -> None:
        """Atomic write of all contracts to session_{id}.json; rotate index; clear live/.

        Called once by the Conductor after all agents are clean, or once by the gate
        in solo mode after its module loop. Never called per-module mid-loop.
        """
        modules: dict[str, Any] = {}
        if isinstance(contracts, dict):
            for name, value in contracts.items():
                modules[name] = value.model_dump(mode="json") if isinstance(value, ContractSummary) else value
        else:
            for summary in contracts:
                modules[summary.module] = summary.model_dump(mode="json")

        session = self.get_or_create_session()
        payload = {
            "session_id": self.session_id,
            "sequence": session.current_sequence,
            "generation_id": session.current_generation_id,
            "modules": modules,
        }
        self._session_file(self.session_id).write_text(json.dumps(payload, indent=2, default=str))

        index = self._load_index()
        old_latest = index.get("latest_session")
        if old_latest and old_latest != self.session_id:
            index["previous_session"] = old_latest
        index["latest_session"] = self.session_id
        self._write_index(index)

        self._clear_live()

    def _clear_live(self) -> None:
        if not self.live_dir.exists():
            return
        for path in self.live_dir.glob("*.json"):
            path.unlink()

    def retrieve_by_files(self, file_paths: list[str]) -> dict[str, ContractSummary]:
        """Return contracts whose module is referenced by any of the given file paths.

        Bounds context at scale; reads from index → latest_session. The seam for a
        future ChromaDB vector query (``CDMAD_RETRIEVAL_TOP_K``).
        """
        if not file_paths:
            return {}
        index = self._load_index()
        modules = self._read_session_modules(index.get("latest_session"))
        result: dict[str, ContractSummary] = {}
        for module, entry in modules.items():
            if any(module in fp or fp.split("/")[0] == module for fp in file_paths):
                summary = self._to_summary(entry)
                if summary is not None:
                    result[module] = summary
        return result

    def write_expected_interfaces(self, interfaces: dict[str, Any]) -> None:
        """Conductor baseline written after the codebase scan."""
        (self.repo_dir / "expected_interfaces.json").write_text(json.dumps(interfaces, indent=2, default=str))

    def write_dependency_graph(self, graph: dict[str, Any]) -> None:
        """Parse order + dependency map written after reading agent prompts."""
        (self.repo_dir / "dependency_graph.json").write_text(json.dumps(graph, indent=2, default=str))

    # ── Checkpoints ──────────────────────────────────────────────

    def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)
        filename = f"checkpoint_{checkpoint.session_id}_{checkpoint.sequence:04d}.json"
        (self.checkpoints_dir / filename).write_text(checkpoint.model_dump_json(indent=2))

    def load_latest_checkpoint(self, session_id: str) -> Checkpoint | None:
        if not self.checkpoints_dir.exists():
            return None
        files = sorted(self.checkpoints_dir.glob(f"checkpoint_{session_id}_*.json"))
        if not files:
            return None
        return Checkpoint.model_validate_json(files[-1].read_text())

    # ── Session ──────────────────────────────────────────────────

    def load_session(self) -> Session | None:
        if not self.session_state_file.exists():
            return None
        try:
            return Session.model_validate_json(self.session_state_file.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def save_session(self, session: Session) -> None:
        self.session_state_file.write_text(session.model_dump_json(indent=2))

    def get_or_create_session(self) -> Session:
        session = self.load_session()
        if session is not None:
            return session
        gen_id = f"gen_{self.session_id}_001"
        session = Session(
            session_id=self.session_id,
            current_generation_id=gen_id,
            current_sequence=0,
        )
        self.save_session(session)
        return session

    def advance_session(self, session: Session) -> Session:
        session.current_sequence += 1
        seq = session.current_sequence
        session.current_generation_id = f"gen_{session.session_id}_{seq:03d}"
        self.save_session(session)
        return session
