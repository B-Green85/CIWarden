"""Tests for scripts.migrate_contracts — JSON ContractStore → VDB migration."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from memory.contract_store import ContractStore
from memory.models import ContractEntry, ContractSummary, ContractType
from scripts.migrate_contracts import plan_migration, run_migration

if TYPE_CHECKING:
    from pathlib import Path


def _save(store: ContractStore, module: str, sid: str, seq: int, names: list[str]) -> None:
    store.save_contract(
        ContractSummary(
            module=module,
            generation_id=f"gen_{sid}_{seq:03d}",
            sequence=seq,
            contracts=[ContractEntry(type=ContractType.FUNCTION, name=n) for n in names],
        )
    )


@pytest.fixture()
def populated(tmp_path: Path) -> Path:
    cdmad = tmp_path / ".cdmad"
    store = ContractStore(root=cdmad)
    # session AAA: mem (two seqs), sched ; session BBB: mem (later)
    _save(store, "mem", "AAA", 1, ["a"])
    _save(store, "mem", "AAA", 2, ["a", "b"])
    _save(store, "sched", "AAA", 2, ["run"])
    _save(store, "mem", "BBB", 3, ["a", "b", "c"])
    return cdmad


class TestPlan:
    def test_groups_by_session_latest_per_module(self, populated: Path) -> None:
        plan = plan_migration(ContractStore(root=populated))
        assert set(plan) == {"AAA", "BBB"}
        assert sorted(plan["AAA"]) == ["mem", "sched"]
        assert plan["AAA"]["mem"].sequence == 2  # latest within session


class TestMigrate:
    def test_no_contract_loss(self, populated: Path) -> None:
        run_migration(root=populated, repo_name="ci-wrapper")
        vdb = populated / "vdb" / "ci-wrapper"
        aaa = json.loads((vdb / "session_AAA.json").read_text())
        assert sorted(aaa["modules"]) == ["mem", "sched"]

    def test_sequence_preserved(self, populated: Path) -> None:
        run_migration(root=populated, repo_name="ci-wrapper")
        vdb = populated / "vdb" / "ci-wrapper"
        aaa = json.loads((vdb / "session_AAA.json").read_text())
        assert aaa["sequence"] == 2
        assert aaa["modules"]["mem"]["sequence"] == 2

    def test_index_pointers(self, populated: Path) -> None:
        run_migration(root=populated, repo_name="ci-wrapper")
        idx = json.loads((populated / "vdb" / "ci-wrapper" / "index.json").read_text())
        # BBB has the highest sequence → latest; AAA → previous.
        assert idx == {"latest_session": "BBB", "previous_session": "AAA"}

    def test_archive_created_and_originals_preserved(self, populated: Path) -> None:
        before = (populated / "contracts" / "mem" / "gen_0002.json").read_text()
        run_migration(root=populated, repo_name="ci-wrapper")
        assert (populated / "contracts_archive").exists()
        assert not (populated / "contracts").exists()
        after = (populated / "contracts_archive" / "mem" / "gen_0002.json").read_text()
        assert before == after

    def test_receipt_written(self, populated: Path) -> None:
        run_migration(root=populated, repo_name="ci-wrapper")
        receipts = list(populated.glob("migration_*.json"))
        assert len(receipts) == 1
        data = json.loads(receipts[0].read_text())
        assert data["repo"] == "ci-wrapper"
        assert data["session_count"] == 2

    def test_idempotent(self, populated: Path) -> None:
        run_migration(root=populated, repo_name="ci-wrapper")
        idx1 = (populated / "vdb" / "ci-wrapper" / "index.json").read_text()
        result = run_migration(root=populated, repo_name="ci-wrapper")  # nothing left
        assert result["session_count"] == 0
        idx2 = (populated / "vdb" / "ci-wrapper" / "index.json").read_text()
        assert idx1 == idx2


class TestDryRun:
    def test_writes_nothing(self, populated: Path) -> None:
        result = run_migration(root=populated, repo_name="ci-wrapper", dry_run=True)
        assert result["dry_run"] is True
        assert not (populated / "vdb").exists()
        assert (populated / "contracts").exists()  # not archived
        assert not list(populated.glob("migration_*.json"))
