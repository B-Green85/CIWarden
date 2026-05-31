"""Tests for memory.contract_store.VectorContractStore — JSON-backed VDB."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from memory.contract_store import VectorContractStore
from memory.models import ContractEntry, ContractSummary, ContractType

if TYPE_CHECKING:
    from pathlib import Path


def _summary(module: str, names: list[str], seq: int) -> ContractSummary:
    return ContractSummary(
        module=module,
        generation_id=f"gen_{seq}",
        sequence=seq,
        contracts=[ContractEntry(type=ContractType.FUNCTION, name=n) for n in names],
    )


@pytest.fixture()
def store(tmp_path: Path) -> VectorContractStore:
    return VectorContractStore("repoA", vdb_root=tmp_path / "vdb", session_id="S1", agent_id="ag1")


class TestRoundTrip:
    def test_save_then_load_latest(self, store: VectorContractStore) -> None:
        store.save_contract(_summary("mem", ["a", "b"], 1))
        store.commit_session([_summary("mem", ["a", "b"], 1)])
        loaded = store.load_latest_contract("mem")
        assert loaded is not None
        assert [c.name for c in loaded.contracts] == ["a", "b"]

    def test_commit_clears_live_and_updates_index(self, store: VectorContractStore) -> None:
        store.save_contract(_summary("mem", ["a"], 1))
        assert list(store.live_dir.glob("*.json"))
        store.commit_session([_summary("mem", ["a"], 1)])
        assert not list(store.live_dir.glob("*.json"))
        assert store._load_index()["latest_session"] == "S1"

    def test_live_keying_no_collision(self, store: VectorContractStore) -> None:
        store.save_contract(_summary("mem", ["a"], 1))
        store.save_contract(_summary("sched", ["x"], 1))
        names = sorted(p.name for p in store.live_dir.glob("*.json"))
        assert names == ["ag1__mem.json", "ag1__sched.json"]


class TestSessionRotation:
    def test_previous_pointer(self, tmp_path: Path) -> None:
        root = tmp_path / "vdb"
        s1 = VectorContractStore("R", vdb_root=root, session_id="S1", agent_id="ag")
        s1.commit_session([_summary("mem", ["a", "b"], 1)])
        s2 = VectorContractStore("R", vdb_root=root, session_id="S2", agent_id="ag")
        s2.commit_session([_summary("mem", ["a", "b", "c"], 2)])
        idx = s2._load_index()
        assert idx == {"latest_session": "S2", "previous_session": "S1"}
        latest = s2.load_latest_contract("mem")
        previous = s2.load_previous_contract("mem", 99)
        assert latest is not None
        assert previous is not None
        assert [c.name for c in latest.contracts] == ["a", "b", "c"]
        assert [c.name for c in previous.contracts] == ["a", "b"]

    def test_load_previous_with_before_sequence_fallback(self, store: VectorContractStore) -> None:
        # No previous pointer yet → fallback scan honors before_sequence.
        # advance_session so the committed payload carries sequence=1 (the real flow).
        store.advance_session(store.get_or_create_session())
        store.commit_session([_summary("mem", ["a"], 1)])
        assert store.load_previous_contract("mem", before_sequence=2) is not None
        assert store.load_previous_contract("mem", before_sequence=1) is None


class TestIsolation:
    def test_repo_isolation(self, tmp_path: Path) -> None:
        root = tmp_path / "vdb"
        a = VectorContractStore("repoA", vdb_root=root, session_id="A", agent_id="ag")
        a.commit_session([_summary("mem", ["a"], 1)])
        b = VectorContractStore("repoB", vdb_root=root, session_id="B", agent_id="ag")
        assert b.load_latest_contract("mem") is None

    def test_session_isolation(self, tmp_path: Path) -> None:
        root = tmp_path / "vdb"
        a = VectorContractStore("R", vdb_root=root, session_id="abc", agent_id="ag")
        a.commit_session([_summary("mem", ["a"], 1)])
        b = VectorContractStore("R", vdb_root=root, session_id="def", agent_id="ag")
        b.commit_session([_summary("sched", ["x"], 2)])
        # Distinct session files exist; abc not overwritten by def.
        assert (a.repo_dir / "session_abc.json").exists()
        assert (a.repo_dir / "session_def.json").exists()


class TestConductorMethods:
    def test_write_expected_interfaces(self, store: VectorContractStore) -> None:
        store.write_expected_interfaces({"mem": {"deliverables": ["init"]}})
        assert (store.repo_dir / "expected_interfaces.json").exists()

    def test_write_dependency_graph(self, store: VectorContractStore) -> None:
        store.write_dependency_graph({"sched": ["mem"]})
        assert (store.repo_dir / "dependency_graph.json").exists()

    def test_retrieve_by_files(self, store: VectorContractStore) -> None:
        store.commit_session([_summary("mem", ["a"], 1), _summary("sched", ["x"], 1)])
        result = store.retrieve_by_files(["mem/core.py"])
        assert set(result) == {"mem"}

    def test_retrieve_by_files_empty(self, store: VectorContractStore) -> None:
        store.commit_session([_summary("mem", ["a"], 1)])
        assert store.retrieve_by_files([]) == {}


class TestStaleIndexFallback:
    def test_fallback_scan(self, store: VectorContractStore) -> None:
        store.commit_session([_summary("mem", ["a"], 3)])
        # Corrupt the index → load_latest must fall back to scanning session files.
        store.index_file.write_text("{}")
        loaded = store.load_latest_contract("mem")
        assert loaded is not None
        assert [c.name for c in loaded.contracts] == ["a"]


class TestMisc:
    def test_root_alias(self, store: VectorContractStore) -> None:
        assert store.root == store.repo_dir

    def test_dir_created_on_first_use(self, tmp_path: Path) -> None:
        s = VectorContractStore("New", vdb_root=tmp_path / "fresh", agent_id="ag")
        assert s.repo_dir.exists()
        assert s.live_dir.exists()

    def test_list_contracts_ordered(self, tmp_path: Path) -> None:
        root = tmp_path / "vdb"
        VectorContractStore("R", vdb_root=root, session_id="S1", agent_id="ag").commit_session(
            [_summary("mem", ["a"], 1)]
        )
        VectorContractStore("R", vdb_root=root, session_id="S2", agent_id="ag").commit_session(
            [_summary("mem", ["a", "b"], 2)]
        )
        reader = VectorContractStore("R", vdb_root=root, session_id="S3", agent_id="ag")
        assert [c.sequence for c in reader.list_contracts("mem")] == [1, 2]
