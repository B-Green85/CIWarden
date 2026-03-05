"""Contract storage interface and implementations for the Generational Memory Gate."""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from pathlib import Path

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


# ── Vector store stub ───────────────────────────────────────────


class VectorContractStore(BaseContractStore):
    """Vector database contract store — ChromaDB / Pinecone drop-in point.

    This is the scaffold for semantic contract storage. When implemented,
    contracts will be embedded and stored as vectors, enabling:
    - Semantic similarity search across contracts (not just exact match)
    - Cross-module contract relationship discovery
    - Fuzzy drift detection based on embedding distance
    - Natural language queries against the contract corpus

    To implement, replace the NotImplementedError bodies with your
    vector DB client calls. The MemoryGate accepts any BaseContractStore
    via constructor injection — no wiring changes needed.
    """

    def __init__(self, collection_name: str = "contracts", **kwargs: object) -> None:
        self.collection_name = collection_name
        self.kwargs = kwargs

    def save_contract(self, summary: ContractSummary) -> None:
        raise NotImplementedError("VectorContractStore.save_contract")

    def load_latest_contract(self, module: str) -> ContractSummary | None:
        raise NotImplementedError("VectorContractStore.load_latest_contract")

    def load_previous_contract(self, module: str, before_sequence: int) -> ContractSummary | None:
        raise NotImplementedError("VectorContractStore.load_previous_contract")

    def list_contracts(self, module: str) -> list[ContractSummary]:
        raise NotImplementedError("VectorContractStore.list_contracts")

    def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        raise NotImplementedError("VectorContractStore.save_checkpoint")

    def load_latest_checkpoint(self, session_id: str) -> Checkpoint | None:
        raise NotImplementedError("VectorContractStore.load_latest_checkpoint")

    def get_or_create_session(self) -> Session:
        raise NotImplementedError("VectorContractStore.get_or_create_session")

    def save_session(self, session: Session) -> None:
        raise NotImplementedError("VectorContractStore.save_session")

    def advance_session(self, session: Session) -> Session:
        raise NotImplementedError("VectorContractStore.advance_session")
