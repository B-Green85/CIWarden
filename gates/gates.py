"""
Gate definitions — each runs as its own FastAPI microservice.
Start individually or use scripts/start_gates.py to launch all.
"""
from __future__ import annotations

import importlib
import os
import sys
from typing import TYPE_CHECKING, Any

import uvicorn

if TYPE_CHECKING:
    from fastapi import FastAPI

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from gates.base_gate import BaseGate
from gates.stress_gate import StressConfig, StressGate

_BUILD_SCRIPT = "import compileall; raise SystemExit(0 if compileall.compile_dir('.', quiet=1) else 1)"


def _is_cpp_project() -> bool:
    """Detect C++ project by presence of CMakeLists.txt in project root."""
    return os.path.isfile(os.path.join(os.getcwd(), "CMakeLists.txt"))


def _build_gate_registry() -> dict[str, dict[str, Any]]:
    """Build gate registry based on detected project type."""
    if _is_cpp_project():
        registry: dict[str, dict[str, Any]] = {
            "lint": {
                "command": [
                    "clang-tidy",
                    "--checks=clang-analyzer-*,modernize-*,performance-*",
                    "-p", "build/",
                    "src/",
                ],
                "port": 8001,
                "phase": "parallel",
                "description": "Static analysis & style enforcement (C++)",
            },
            "typecheck": {
                "command": ["cmake", "--build", "build/", "--target", "bt_engine"],
                "port": 8002,
                "phase": "parallel",
                "description": "Type safety verification (compiler)",
            },
            "security": {
                "command": [
                    "cppcheck",
                    "--enable=warning,performance,portability",
                    "--error-exitcode=1",
                    "src/",
                ],
                "port": 8003,
                "phase": "parallel",
                "description": "Security & static analysis (C++)",
            },
            "test": {
                "command": [
                    "ctest",
                    "--test-dir", "build/",
                    "--output-on-failure",
                    "-R", "bt_tests",
                ],
                "port": 8004,
                "phase": "sequential",
                "order": 1,
                "description": "Unit & integration test suite (C++)",
            },
            "build": {
                "command": ["cmake", "--build", "build/", "--target", "bt_tests"],
                "port": 8005,
                "phase": "sequential",
                "order": 3,
                "description": "Compilation & build validation (C++)",
            },
        }
    else:
        # Python project gates (default)
        registry = {
            "lint": {
                "command": ["ruff", "check", "."],
                "port": 8001,
                "phase": "parallel",
                "description": "Static analysis & style enforcement",
            },
            "typecheck": {
                "command": ["mypy", ".", "--ignore-missing-imports"],
                "port": 8002,
                "phase": "parallel",
                "description": "Type safety verification",
            },
            "security": {
                "command": ["bandit", "-r", ".", "-ll", "-q"],
                "port": 8003,
                "phase": "parallel",
                "description": "Security & dependency audit",
            },
            "test": {
                "command": ["pytest", "--tb=short", "-q"],
                "port": 8004,
                "phase": "sequential",
                "order": 1,
                "description": "Unit & integration test suite",
            },
            "build": {
                "command": [sys.executable, "-c", _BUILD_SCRIPT],
                "port": 8005,
                "phase": "sequential",
                "order": 3,
                "description": "Compilation & build validation",
            },
        }

    # Shared gates — available for all project types
    registry["memory"] = {
        "command": [],
        "port": 8006,
        "phase": "parallel",
        "description": "Generational memory — architectural drift detection",
        "custom_app": "memory.app:app",
    }
    registry["stress"] = {
        "command": [],
        "port": 8007,
        "phase": "sequential",
        "order": 2,
        "description": "API stress testing — three-phase load runner",
        "gate_class": "StressGate",
    }
    return registry


# ── Gate definitions ──────────────────────────────────────────
GATE_REGISTRY: dict[str, dict[str, Any]] = _build_gate_registry()


def get_gate_app(gate_name: str) -> FastAPI:
    """Return the FastAPI app for a given gate name."""
    config = GATE_REGISTRY[gate_name]
    if "custom_app" in config:
        module_path, attr = str(config["custom_app"]).rsplit(":", 1)
        mod = importlib.import_module(module_path)
        return getattr(mod, attr)  # type: ignore[no-any-return]
    if config.get("gate_class") == "StressGate":
        gate: BaseGate = StressGate(
            name=gate_name,
            port=int(config["port"]),
            config=StressConfig(),
        )
        return gate.app
    gate = BaseGate(
        name=gate_name,
        command=list(config["command"]),
        port=int(config["port"]),
    )
    return gate.app


def run_gate(gate_name: str) -> None:
    """Run a single gate as a standalone service."""
    config = GATE_REGISTRY[gate_name]
    app = get_gate_app(gate_name)
    print(f"[GATE] Starting {gate_name} on port {config['port']}")
    uvicorn.run(app, host="0.0.0.0", port=int(config["port"]), log_level="warning")  # nosec B104


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python gates.py <gate_name>")
        print(f"Available gates: {list(GATE_REGISTRY.keys())}")
        sys.exit(1)
    run_gate(sys.argv[1])
