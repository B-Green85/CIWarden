"""Tests for memory.peer_checker — intra-session cross-check (Tier 1)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from memory.contract_store import VectorContractStore
from memory.peer_checker import PeerChecker, _assumptions_contradict

MEM_SIG = "pub fn init(memory_map: *const ()) -> Result<(), KernelError>"
REPORT_DIR = Path("conductor_staging/agent_003")  # cosmetic path in report assertions


@pytest.fixture()
def store(tmp_path: Path) -> VectorContractStore:
    return VectorContractStore("R", vdb_root=tmp_path / "vdb", session_id="S", agent_id="ag")


def _write_live(store: VectorContractStore, agent: str, module: str, schema: dict[str, Any]) -> None:
    store.live_dir.mkdir(parents=True, exist_ok=True)
    (store.live_dir / f"{agent}__{module}.json").write_text(json.dumps(schema))


def _modules(entry: dict[str, Any], module: str = "m") -> dict[str, Any]:
    return {"modules": {module: entry}}


def _consumes(sig: str, symbol: str = "memory::init") -> dict[str, Any]:
    return _modules({"consumes": [{"symbol": symbol, "expected_signature": sig}]})


def _expose_mem(store: VectorContractStore, agent: str = "agent_002") -> None:
    _write_live(store, agent, "memory", {"exposes": [{"symbol": "memory::init", "signature": MEM_SIG}]})


class TestCollision:
    def test_same_symbol_two_agents(self, store: VectorContractStore) -> None:
        _write_live(store, "agent_002", "memory", {"exposes": [{"symbol": "memory::init", "signature": MEM_SIG}]})
        pc = PeerChecker(store)
        conflicts = pc.check("agent_004", _modules({"exposes": [{"symbol": "memory::init", "signature": "x"}]}))
        assert any(c.kind == "COLLISION" and c.symbol == "memory::init" for c in conflicts)

    def test_different_symbols_no_conflict(self, store: VectorContractStore) -> None:
        _write_live(store, "agent_002", "memory", {"exposes": [{"symbol": "memory::init", "signature": MEM_SIG}]})
        pc = PeerChecker(store)
        conflicts = pc.check("agent_004", _modules({"exposes": [{"symbol": "fs::open", "signature": "y"}]}))
        assert not conflicts


class TestInterfaceMismatch:
    def test_wrong_return_type(self, store: VectorContractStore) -> None:
        _expose_mem(store)
        bad = "pub fn init(memory_map: *const ()) -> Result<(), &'static str>"
        conflicts = PeerChecker(store).check("agent_003", _consumes(bad))
        assert any(c.kind == "INTERFACE_MISMATCH" for c in conflicts)

    def test_symbol_not_exposed(self, store: VectorContractStore) -> None:
        conflicts = PeerChecker(store).check("agent_003", _consumes("fn open()", symbol="fs::open"))
        assert any(c.kind == "INTERFACE_MISMATCH" and c.peer_id == "(none)" for c in conflicts)

    def test_matching_signature_clean(self, store: VectorContractStore) -> None:
        _expose_mem(store)
        conflicts = PeerChecker(store).check("agent_003", _consumes(MEM_SIG))
        assert not conflicts


class TestAssumptionClash:
    def test_no_std_vs_std(self, store: VectorContractStore) -> None:
        _write_live(store, "agent_002", "memory", {"exposes": [], "assumptions": ["no_std"]})
        pc = PeerChecker(store)
        conflicts = pc.check("agent_003", _modules({"exposes": [], "assumptions": ["std"]}))
        assert any(c.kind == "ASSUMPTION_CLASH" for c in conflicts)

    def test_compatible_assumptions_clean(self, store: VectorContractStore) -> None:
        _write_live(store, "agent_002", "memory", {"exposes": [], "assumptions": ["no_std"]})
        pc = PeerChecker(store)
        conflicts = pc.check("agent_003", _modules({"exposes": [], "assumptions": ["no_std"]}))
        assert not conflicts


class TestReadAllLive:
    def test_reads_all(self, store: VectorContractStore) -> None:
        _write_live(store, "agent_001", "boot", {"exposes": [{"symbol": "boot::start", "signature": "s"}]})
        _write_live(store, "agent_002", "memory", {"exposes": [{"symbol": "memory::init", "signature": "i"}]})
        views = PeerChecker(store).read_all_live()
        assert set(views) == {"agent_001", "agent_002"}

    def test_empty(self, store: VectorContractStore) -> None:
        assert PeerChecker(store).read_all_live() == {}


class TestConflictReport:
    def _report(self, store: VectorContractStore, attempt: int) -> str:
        _expose_mem(store)
        bad = "pub fn init() -> Result<(), &'static str>"
        conflicts = PeerChecker(store).check("agent_003", _consumes(bad))
        return PeerChecker(store).format_conflict_report(
            "agent_003", "src/scheduler/", conflicts, attempt, REPORT_DIR
        )

    def test_attempt1_no_code_block(self, store: VectorContractStore) -> None:
        report = self._report(store, 1)
        assert "Exact code:" not in report
        assert "Correct signature:" not in report

    def test_attempt2_has_signature(self, store: VectorContractStore) -> None:
        report = self._report(store, 2)
        assert "Correct signature:" in report
        assert "Exact code:" not in report

    def test_attempt3_has_code_block(self, store: VectorContractStore) -> None:
        assert "Exact code:" in self._report(store, 3)


class TestAssumptionsContradict:
    def test_no_std_std(self) -> None:
        assert _assumptions_contradict("no_std", "std")

    def test_negation(self) -> None:
        assert _assumptions_contradict("locking enabled", "not locking enabled")

    def test_unrelated(self) -> None:
        assert not _assumptions_contradict("x86_64", "no_std")
