"""Tests for gates.gates — gate registry and launcher."""

import pytest

from gates.gates import GATE_REGISTRY, get_gate_app


class TestGateRegistry:
    def test_all_required_gates_present(self) -> None:
        expected = {"lint", "typecheck", "security", "test", "build", "memory", "stress"}
        assert set(GATE_REGISTRY.keys()) == expected

    def test_each_gate_has_required_keys(self) -> None:
        required_keys = {"command", "port", "phase", "description"}
        for name, config in GATE_REGISTRY.items():
            assert required_keys.issubset(config.keys()), f"{name} missing keys"

    def test_ports_are_unique(self) -> None:
        ports = [config["port"] for config in GATE_REGISTRY.values()]
        assert len(ports) == len(set(ports)), "Duplicate ports in registry"

    def test_phases_are_valid(self) -> None:
        valid_phases = {"parallel", "sequential"}
        for name, config in GATE_REGISTRY.items():
            assert config["phase"] in valid_phases, f"{name} has invalid phase"

    def test_commands_are_lists(self) -> None:
        for name, config in GATE_REGISTRY.items():
            assert isinstance(config["command"], list), f"{name} command not a list"

    def test_parallel_gates(self) -> None:
        parallel = [k for k, v in GATE_REGISTRY.items() if v["phase"] == "parallel"]
        assert set(parallel) == {"lint", "typecheck", "security", "memory"}

    def test_sequential_gates(self) -> None:
        sequential = [k for k, v in GATE_REGISTRY.items() if v["phase"] == "sequential"]
        assert set(sequential) == {"test", "build", "stress"}

    def test_memory_gate_has_custom_app(self) -> None:
        assert "custom_app" in GATE_REGISTRY["memory"]
        assert GATE_REGISTRY["memory"]["custom_app"] == "memory.app:app"

    def test_stress_gate_has_gate_class(self) -> None:
        assert GATE_REGISTRY["stress"]["gate_class"] == "StressGate"
        assert GATE_REGISTRY["stress"]["port"] == 8007

    def test_sequential_gate_ordering(self) -> None:
        sequential = sorted(
            [k for k, v in GATE_REGISTRY.items() if v["phase"] == "sequential"],
            key=lambda k: int(GATE_REGISTRY[k].get("order", 99)),
        )
        assert sequential == ["test", "stress", "build"]


class TestGetGateApp:
    def test_returns_fastapi_app(self) -> None:
        app = get_gate_app("lint")
        assert app is not None
        assert hasattr(app, "routes")

    def test_unknown_gate_raises(self) -> None:
        with pytest.raises(KeyError):
            get_gate_app("nonexistent")
