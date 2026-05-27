"""Tests for memory.memory_gate — core gate logic."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from gates.base_gate import GateStatus
from memory.contract_store import ContractStore
from memory.drift_scorer import DRIFT_THRESHOLD
from memory.llm_client import LLMClient
from memory.memory_gate import MemoryGate, _compute_source_sha
from memory.models import ContractEntry, ContractSummary, ContractType

if TYPE_CHECKING:
    from pathlib import Path


class MockLLMClient(LLMClient):
    """Mock LLM client that returns predictable contracts."""

    def __init__(self, contracts: list[ContractEntry] | None = None) -> None:
        self._contracts = contracts or [
            ContractEntry(type=ContractType.CLASS, name="DefaultClass")
        ]

    async def extract_contracts(
        self,
        source_code: str,
        module_name: str,
        generation_id: str,
        sequence: int,
    ) -> ContractSummary:
        return ContractSummary(
            module=module_name,
            generation_id=generation_id,
            sequence=sequence,
            contracts=self._contracts,
            assumptions=["test assumption"],
            dependencies=["test dep"],
            exposes=[c.name for c in self._contracts],
        )


class HighDriftLLMClient(LLMClient):
    """Mock LLM client that returns contracts with configurable assumptions/deps."""

    def __init__(
        self,
        contracts: list[ContractEntry],
        assumptions: list[str] | None = None,
        dependencies: list[str] | None = None,
    ) -> None:
        self._contracts = contracts
        self._assumptions = assumptions or []
        self._dependencies = dependencies or []

    async def extract_contracts(
        self,
        source_code: str,
        module_name: str,
        generation_id: str,
        sequence: int,
    ) -> ContractSummary:
        return ContractSummary(
            module=module_name,
            generation_id=generation_id,
            sequence=sequence,
            contracts=self._contracts,
            assumptions=self._assumptions,
            dependencies=self._dependencies,
            exposes=[c.name for c in self._contracts],
        )


class FailingLLMClient(LLMClient):
    """Mock LLM client that always raises."""

    async def extract_contracts(
        self,
        source_code: str,
        module_name: str,
        generation_id: str,
        sequence: int,
    ) -> ContractSummary:
        msg = "API connection failed"
        raise RuntimeError(msg)


@pytest.fixture()
def store(tmp_path: Path) -> ContractStore:
    return ContractStore(root=tmp_path / ".cdmad")


class TestMemoryGateFirstGeneration:
    @pytest.mark.asyncio
    async def test_first_generation_passes(self, store: ContractStore) -> None:
        gate = MemoryGate(llm_client=MockLLMClient(), store=store)
        result = await gate.run(source_files={"gates": "class Foo: pass"})

        assert result.status == GateStatus.PASS
        assert result.exit_code == 0
        assert "first generation" in result.output

    @pytest.mark.asyncio
    async def test_no_source_files_passes(self, store: ContractStore) -> None:
        gate = MemoryGate(llm_client=MockLLMClient(), store=store)
        result = await gate.run(source_files={})

        assert result.status == GateStatus.PASS
        assert "No source files" in result.output


class TestMemoryGateDrift:
    @pytest.mark.asyncio
    async def test_low_drift_passes(self, store: ContractStore) -> None:
        # First generation — establishes baseline
        gate = MemoryGate(llm_client=MockLLMClient(), store=store)
        r1 = await gate.run(source_files={"mod": "class A: pass"})
        assert r1.status == GateStatus.PASS

        # Second generation — same contracts → zero drift
        r2 = await gate.run(source_files={"mod": "class A: pass"})
        assert r2.status == GateStatus.PASS
        assert r2.exit_code == 0

    @pytest.mark.asyncio
    async def test_high_drift_blocks(self, store: ContractStore) -> None:
        # First generation — 3 contracts with fields
        client1 = HighDriftLLMClient(
            contracts=[
                ContractEntry(type=ContractType.CLASS, name="Foo", fields=["x"]),
                ContractEntry(type=ContractType.CLASS, name="Bar", fields=["x"]),
                ContractEntry(type=ContractType.CLASS, name="Baz"),
            ],
            assumptions=["metric units"],
            dependencies=["fastapi"],
        )
        gate = MemoryGate(llm_client=client1, store=store)
        await gate.run(source_files={"mod": "code"})

        # Second generation — 1 removed + 2 modified fields → drift > 0.5
        client2 = HighDriftLLMClient(
            contracts=[
                ContractEntry(type=ContractType.CLASS, name="Foo", fields=["y"]),
                ContractEntry(type=ContractType.CLASS, name="Bar", fields=["y"]),
            ],
            assumptions=["imperial units"],
            dependencies=["flask"],
        )
        gate2 = MemoryGate(llm_client=client2, store=store)
        result = await gate2.run(source_files={"mod": "code"})

        assert result.status == GateStatus.FAIL
        assert result.exit_code == 1
        assert "BLOCKED" in result.output


class TestMemoryGateErrors:
    @pytest.mark.asyncio
    async def test_llm_failure_returns_fail(self, store: ContractStore) -> None:
        gate = MemoryGate(llm_client=FailingLLMClient(), store=store)
        result = await gate.run(source_files={"mod": "code"})

        assert result.status == GateStatus.FAIL
        assert "error" in result.output.lower()

    @pytest.mark.asyncio
    async def test_session_advances_sequence(self, store: ContractStore) -> None:
        gate = MemoryGate(llm_client=MockLLMClient(), store=store)
        await gate.run(source_files={"mod": "code"})
        await gate.run(source_files={"mod": "code"})

        session = store.load_session()
        assert session is not None
        assert session.current_sequence == 2


class TestMemoryGateThresholdOverride:
    def test_default_threshold(self, store: ContractStore) -> None:
        gate = MemoryGate(llm_client=MockLLMClient(), store=store)
        assert gate.threshold == DRIFT_THRESHOLD

    def test_constructor_override(self, store: ContractStore) -> None:
        gate = MemoryGate(llm_client=MockLLMClient(), store=store, drift_threshold=0.8)
        assert gate.threshold == 0.8

    def test_env_var_override(self, store: ContractStore) -> None:
        with patch.dict("os.environ", {"CDMAD_DRIFT_THRESHOLD": "0.7"}):
            gate = MemoryGate(llm_client=MockLLMClient(), store=store)
        assert gate.threshold == 0.7

    def test_env_var_takes_precedence_over_constructor(self, store: ContractStore) -> None:
        with patch.dict("os.environ", {"CDMAD_DRIFT_THRESHOLD": "0.2"}):
            gate = MemoryGate(llm_client=MockLLMClient(), store=store, drift_threshold=0.9)
        assert gate.threshold == 0.2


class TestSourceShaCache:
    def test_compute_source_sha_deterministic(self) -> None:
        files = {"mod_a": "code_a", "mod_b": "code_b"}
        assert _compute_source_sha(files) == _compute_source_sha(files)

    def test_compute_source_sha_order_independent(self) -> None:
        a = {"mod_a": "code_a", "mod_b": "code_b"}
        b = {"mod_b": "code_b", "mod_a": "code_a"}
        assert _compute_source_sha(a) == _compute_source_sha(b)

    def test_compute_source_sha_changes_on_content_change(self) -> None:
        a = {"mod": "version1"}
        b = {"mod": "version2"}
        assert _compute_source_sha(a) != _compute_source_sha(b)

    @pytest.mark.asyncio
    async def test_cache_disabled_by_default(self, store: ContractStore) -> None:
        gate = MemoryGate(llm_client=MockLLMClient(), store=store)
        assert not gate._cache_enabled

    @pytest.mark.asyncio
    async def test_cache_skips_on_same_source(self, store: ContractStore) -> None:
        with patch.dict("os.environ", {"CDMAD_MEMORY_CACHE": "1"}):
            gate = MemoryGate(llm_client=MockLLMClient(), store=store)
            files = {"mod": "class A: pass"}

            r1 = await gate.run(source_files=files)
            assert r1.status == GateStatus.PASS
            assert "CACHED" not in r1.output

            r2 = await gate.run(source_files=files)
            assert r2.status == GateStatus.PASS
            assert "CACHED" in r2.output

    @pytest.mark.asyncio
    async def test_cache_reruns_on_changed_source(self, store: ContractStore) -> None:
        with patch.dict("os.environ", {"CDMAD_MEMORY_CACHE": "1"}):
            gate = MemoryGate(llm_client=MockLLMClient(), store=store)

            await gate.run(source_files={"mod": "class A: pass"})
            r2 = await gate.run(source_files={"mod": "class B: pass"})

            assert r2.status == GateStatus.PASS
            assert "CACHED" not in r2.output

    @pytest.mark.asyncio
    async def test_cache_not_written_on_fail(self, store: ContractStore) -> None:
        with patch.dict("os.environ", {"CDMAD_MEMORY_CACHE": "1"}):
            # First gen — establish baseline with 3 contracts
            client1 = HighDriftLLMClient(
                contracts=[
                    ContractEntry(type=ContractType.CLASS, name="Foo", fields=["x"]),
                    ContractEntry(type=ContractType.CLASS, name="Bar", fields=["x"]),
                    ContractEntry(type=ContractType.CLASS, name="Baz"),
                ],
                assumptions=["metric units"],
                dependencies=["fastapi"],
            )
            gate1 = MemoryGate(llm_client=client1, store=store)
            await gate1.run(source_files={"mod": "code"})

            # Second gen — high drift → fail
            client2 = HighDriftLLMClient(
                contracts=[
                    ContractEntry(type=ContractType.CLASS, name="Foo", fields=["y"]),
                    ContractEntry(type=ContractType.CLASS, name="Bar", fields=["y"]),
                ],
                assumptions=["imperial units"],
                dependencies=["flask"],
            )
            gate2 = MemoryGate(llm_client=client2, store=store)
            result = await gate2.run(source_files={"mod": "changed"})
            assert result.status == GateStatus.FAIL

            # Cache file should not contain the failing source's SHA
            cache_path = gate2._cache_path()
            assert cache_path is not None
            failing_sha = _compute_source_sha({"mod": "changed"})
            cached_sha = gate2._read_cached_sha()
            assert cached_sha != failing_sha

    @pytest.mark.asyncio
    async def test_cache_disabled_does_not_skip(self, store: ContractStore) -> None:
        """Without CDMAD_MEMORY_CACHE=1, no caching occurs even with identical sources."""
        gate = MemoryGate(llm_client=MockLLMClient(), store=store)
        files = {"mod": "class A: pass"}

        await gate.run(source_files=files)
        r2 = await gate.run(source_files=files)

        assert r2.status == GateStatus.PASS
        assert "CACHED" not in r2.output


class TestMemoryGateMultipleModules:
    @pytest.mark.asyncio
    async def test_multiple_modules(self, store: ContractStore) -> None:
        gate = MemoryGate(llm_client=MockLLMClient(), store=store)
        result = await gate.run(source_files={
            "gates": "class Gate: pass",
            "orchestrator": "class Orch: pass",
        })
        assert result.status == GateStatus.PASS
        assert "gates" in result.output
        assert "orchestrator" in result.output
