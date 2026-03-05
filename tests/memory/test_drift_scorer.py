"""Tests for memory.drift_scorer — contract drift comparison."""

from memory.drift_scorer import DRIFT_THRESHOLD, score_drift
from memory.models import ContractEntry, ContractSummary, ContractType


def _summary(
    contracts: list[ContractEntry] | None = None,
    assumptions: list[str] | None = None,
    dependencies: list[str] | None = None,
) -> ContractSummary:
    return ContractSummary(
        module="test",
        generation_id="gen_001",
        sequence=1,
        contracts=contracts or [],
        assumptions=assumptions or [],
        dependencies=dependencies or [],
    )


def _entry(name: str, fields: list[str] | None = None) -> ContractEntry:
    return ContractEntry(type=ContractType.CLASS, name=name, fields=fields or [])


class TestScoreDrift:
    def test_identical_contracts_zero_drift(self) -> None:
        a = _summary(contracts=[_entry("Foo", ["x", "y"])])
        b = _summary(contracts=[_entry("Foo", ["x", "y"])])
        result = score_drift(a, b)
        assert result.score == 0.0
        assert result.passed is True

    def test_empty_contracts_zero_drift(self) -> None:
        result = score_drift(_summary(), _summary())
        assert result.score == 0.0
        assert result.passed is True

    def test_added_contracts(self) -> None:
        a = _summary(contracts=[_entry("Foo")])
        b = _summary(contracts=[_entry("Foo"), _entry("Bar")])
        result = score_drift(a, b)
        assert result.score > 0.0
        assert "class:Bar" in result.added
        assert len(result.removed) == 0

    def test_removed_contracts_higher_than_added(self) -> None:
        added_only = score_drift(
            _summary(contracts=[_entry("Foo")]),
            _summary(contracts=[_entry("Foo"), _entry("Bar")]),
        )
        removed_only = score_drift(
            _summary(contracts=[_entry("Foo"), _entry("Bar")]),
            _summary(contracts=[_entry("Foo")]),
        )
        assert removed_only.score > added_only.score

    def test_modified_contracts(self) -> None:
        a = _summary(contracts=[_entry("Foo", ["x", "y"])])
        b = _summary(contracts=[_entry("Foo", ["x", "z"])])
        result = score_drift(a, b)
        assert result.score > 0.0
        assert "class:Foo" in result.modified

    def test_completely_different_contracts(self) -> None:
        # Mix of removals + modifications yields high drift (>0.5)
        a = _summary(
            contracts=[_entry("Foo", ["x"]), _entry("Bar", ["x"]), _entry("Baz")],
            assumptions=["metric units"],
            dependencies=["fastapi"],
        )
        b = _summary(
            contracts=[_entry("Foo", ["y"]), _entry("Bar", ["y"])],
            assumptions=["imperial units"],
            dependencies=["flask"],
        )
        result = score_drift(a, b)
        assert result.score > DRIFT_THRESHOLD
        assert result.passed is False

    def test_threshold_boundary_pass(self) -> None:
        # Single added contract out of many → low drift
        contracts = [_entry(f"C{i}") for i in range(10)]
        a = _summary(contracts=contracts)
        b = _summary(contracts=contracts + [_entry("New")])
        result = score_drift(a, b)
        assert result.passed is True

    def test_assumption_drift_adds_penalty(self) -> None:
        a = _summary(contracts=[_entry("Foo")], assumptions=["meters"])
        b = _summary(contracts=[_entry("Foo")], assumptions=["feet"])
        result = score_drift(a, b)
        assert result.score > 0.0
        assert result.detail
        assert "Assumption" in result.detail

    def test_dependency_drift_adds_penalty(self) -> None:
        a = _summary(contracts=[_entry("Foo")], dependencies=["fastapi"])
        b = _summary(contracts=[_entry("Foo")], dependencies=["flask"])
        result = score_drift(a, b)
        assert result.score > 0.0
        assert "Dependency" in result.detail

    def test_no_drift_detail_message(self) -> None:
        a = _summary(contracts=[_entry("Foo")])
        b = _summary(contracts=[_entry("Foo")])
        result = score_drift(a, b)
        assert result.detail == "No drift detected."
