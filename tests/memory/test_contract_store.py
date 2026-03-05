"""Tests for memory.contract_store — file-based contract storage."""

from pathlib import Path

import pytest

from memory.contract_store import ContractStore
from memory.models import Checkpoint, ContractEntry, ContractSummary, ContractType, DriftResult


@pytest.fixture()
def store(tmp_path: Path) -> ContractStore:
    return ContractStore(root=tmp_path / ".cdmad")


def _make_summary(module: str, gen_id: str, seq: int) -> ContractSummary:
    return ContractSummary(
        module=module,
        generation_id=gen_id,
        sequence=seq,
        contracts=[ContractEntry(type=ContractType.CLASS, name=f"{module}_Class")],
        assumptions=["test assumption"],
    )


class TestEnsureDirs:
    def test_creates_directories(self, store: ContractStore) -> None:
        store.ensure_dirs()
        assert store.contracts_dir.exists()
        assert store.checkpoints_dir.exists()

    def test_idempotent(self, store: ContractStore) -> None:
        store.ensure_dirs()
        store.ensure_dirs()
        assert store.contracts_dir.exists()


class TestContractCRUD:
    def test_save_and_load(self, store: ContractStore) -> None:
        summary = _make_summary("gates", "gen_001", 1)
        store.save_contract(summary)

        loaded = store.load_contract("gates", "gen_001")
        assert loaded is not None
        assert loaded.module == "gates"
        assert loaded.generation_id == "gen_001"

    def test_load_nonexistent_returns_none(self, store: ContractStore) -> None:
        assert store.load_contract("nope", "gen_999") is None

    def test_load_latest_contract(self, store: ContractStore) -> None:
        store.save_contract(_make_summary("gates", "gen_001", 1))
        store.save_contract(_make_summary("gates", "gen_002", 2))
        store.save_contract(_make_summary("gates", "gen_003", 3))

        latest = store.load_latest_contract("gates")
        assert latest is not None
        assert latest.generation_id == "gen_003"

    def test_load_latest_empty_returns_none(self, store: ContractStore) -> None:
        assert store.load_latest_contract("gates") is None

    def test_load_previous_contract(self, store: ContractStore) -> None:
        store.save_contract(_make_summary("gates", "gen_001", 1))
        store.save_contract(_make_summary("gates", "gen_002", 2))
        store.save_contract(_make_summary("gates", "gen_003", 3))

        prev = store.load_previous_contract("gates", before_sequence=3)
        assert prev is not None
        assert prev.sequence == 2

    def test_load_previous_first_gen_returns_none(self, store: ContractStore) -> None:
        store.save_contract(_make_summary("gates", "gen_001", 1))
        assert store.load_previous_contract("gates", before_sequence=1) is None

    def test_list_contracts(self, store: ContractStore) -> None:
        store.save_contract(_make_summary("gates", "gen_001", 1))
        store.save_contract(_make_summary("gates", "gen_002", 2))

        contracts = store.list_contracts("gates")
        assert len(contracts) == 2
        assert contracts[0].sequence == 1
        assert contracts[1].sequence == 2

    def test_list_contracts_empty(self, store: ContractStore) -> None:
        assert store.list_contracts("gates") == []

    def test_list_modules(self, store: ContractStore) -> None:
        store.save_contract(_make_summary("gates", "gen_001", 1))
        store.save_contract(_make_summary("orchestrator", "gen_001", 1))

        modules = store.list_modules()
        assert modules == ["gates", "orchestrator"]

    def test_list_modules_empty(self, store: ContractStore) -> None:
        assert store.list_modules() == []


class TestCheckpoints:
    def test_save_and_load(self, store: ContractStore) -> None:
        cp = Checkpoint(
            session_id="sess1",
            generation_id="gen_001",
            sequence=1,
            contracts=[_make_summary("gates", "gen_001", 1)],
            drift_history=[DriftResult(score=0.1, passed=True)],
        )
        store.save_checkpoint(cp)

        loaded = store.load_latest_checkpoint("sess1")
        assert loaded is not None
        assert loaded.generation_id == "gen_001"

    def test_load_latest_returns_highest_seq(self, store: ContractStore) -> None:
        store.save_checkpoint(Checkpoint(session_id="s", generation_id="g1", sequence=1))
        store.save_checkpoint(Checkpoint(session_id="s", generation_id="g2", sequence=2))

        latest = store.load_latest_checkpoint("s")
        assert latest is not None
        assert latest.sequence == 2

    def test_load_nonexistent_returns_none(self, store: ContractStore) -> None:
        assert store.load_latest_checkpoint("nope") is None


class TestSession:
    def test_get_or_create_new(self, store: ContractStore) -> None:
        session = store.get_or_create_session()
        assert session.session_id
        assert session.current_sequence == 0

    def test_get_or_create_existing(self, store: ContractStore) -> None:
        s1 = store.get_or_create_session()
        s2 = store.get_or_create_session()
        assert s1.session_id == s2.session_id

    def test_advance_session(self, store: ContractStore) -> None:
        session = store.get_or_create_session()
        assert session.current_sequence == 0

        session = store.advance_session(session)
        assert session.current_sequence == 1
        assert "001" in session.current_generation_id

        session = store.advance_session(session)
        assert session.current_sequence == 2
