"""Tests for memory.models — Pydantic models for the Memory Gate."""

import pytest
from pydantic import ValidationError

from memory.models import (
    Checkpoint,
    ContractEntry,
    ContractSummary,
    ContractType,
    DriftResult,
    Session,
)


class TestContractType:
    def test_enum_values(self) -> None:
        assert ContractType.INTERFACE.value == "interface"
        assert ContractType.FUNCTION.value == "function"
        assert ContractType.CLASS.value == "class"
        assert ContractType.MODULE.value == "module"
        assert ContractType.DEPENDENCY.value == "dependency"


class TestContractEntry:
    def test_minimal(self) -> None:
        entry = ContractEntry(type=ContractType.CLASS, name="Foo")
        assert entry.type == ContractType.CLASS
        assert entry.name == "Foo"
        assert entry.fields == []
        assert entry.consumed_by == []
        assert entry.methods == []

    def test_full(self) -> None:
        entry = ContractEntry(
            type=ContractType.INTERFACE,
            name="FlightData",
            fields=["lat", "lon"],
            consumed_by=["ProcessedFlight"],
            methods=["to_dict"],
        )
        assert len(entry.fields) == 2
        assert "ProcessedFlight" in entry.consumed_by


class TestContractSummary:
    def test_minimal(self) -> None:
        summary = ContractSummary(module="test", generation_id="gen_001", sequence=1)
        assert summary.module == "test"
        assert summary.contracts == []
        assert summary.extracted_at is not None

    def test_serialization_roundtrip(self) -> None:
        summary = ContractSummary(
            module="gates",
            generation_id="gen_002",
            sequence=2,
            contracts=[ContractEntry(type=ContractType.CLASS, name="BaseGate")],
            assumptions=["Python 3.12+"],
            dependencies=["fastapi"],
            exposes=["BaseGate class"],
        )
        json_str = summary.model_dump_json()
        restored = ContractSummary.model_validate_json(json_str)
        assert restored.module == "gates"
        assert len(restored.contracts) == 1
        assert restored.contracts[0].name == "BaseGate"


class TestDriftResult:
    def test_valid_score(self) -> None:
        result = DriftResult(score=0.25, passed=True)
        assert result.score == 0.25

    def test_score_bounds(self) -> None:
        DriftResult(score=0.0, passed=True)
        DriftResult(score=1.0, passed=False)
        with pytest.raises(ValidationError):
            DriftResult(score=-0.1, passed=True)
        with pytest.raises(ValidationError):
            DriftResult(score=1.1, passed=False)


class TestCheckpoint:
    def test_minimal(self) -> None:
        cp = Checkpoint(session_id="abc", generation_id="gen_001", sequence=1)
        assert cp.contracts == []
        assert cp.drift_history == []


class TestSession:
    def test_defaults(self) -> None:
        s = Session(session_id="abc", current_generation_id="gen_abc_001")
        assert s.current_sequence == 0
        assert s.last_checkpoint is None
        assert s.started_at is not None
