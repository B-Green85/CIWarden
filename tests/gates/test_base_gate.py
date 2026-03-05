"""Tests for gates.base_gate — BaseGate microservice wrapper."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from gates.base_gate import BaseGate, GateResult, GateStatus


@pytest.fixture()
def echo_gate() -> BaseGate:
    """A gate that runs 'echo hello' — always passes."""
    return BaseGate(name="echo", command=["echo", "hello"], port=9001)


@pytest.fixture()
def client(echo_gate: BaseGate) -> TestClient:
    return TestClient(echo_gate.app)


class TestGateStatus:
    def test_enum_values(self) -> None:
        assert GateStatus.PASS.value == "pass"
        assert GateStatus.FAIL.value == "fail"
        assert GateStatus.RUNNING.value == "running"


class TestGateResult:
    def test_model_fields(self) -> None:
        result = GateResult(
            gate="lint",
            status=GateStatus.PASS,
            output="ok",
            exit_code=0,
            duration_ms=42,
        )
        assert result.gate == "lint"
        assert result.status == GateStatus.PASS
        assert result.exit_code == 0


class TestBaseGateHealth:
    def test_health_returns_ready(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["gate"] == "echo"
        assert data["status"] == "ready"
        assert data["port"] == 9001


class TestBaseGateRun:
    def test_run_passing_command(self, client: TestClient) -> None:
        resp = client.post("/run")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "pass"
        assert data["exit_code"] == 0
        assert data["duration_ms"] >= 0
        assert "hello" in data["output"]

    def test_run_failing_command(self) -> None:
        gate = BaseGate(name="fail", command=["false"], port=9002)
        client = TestClient(gate.app)
        resp = client.post("/run")
        data = resp.json()
        assert data["status"] == "fail"
        assert data["exit_code"] != 0

    def test_run_missing_tool(self) -> None:
        gate = BaseGate(name="missing", command=["nonexistent_tool_xyz"], port=9003)
        client = TestClient(gate.app)
        resp = client.post("/run")
        data = resp.json()
        assert data["status"] == "fail"
        assert data["exit_code"] == 127
        assert "Tool not found" in data["output"]

    @patch("gates.base_gate.subprocess.run")
    def test_run_captures_stderr(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="error: something broke",
        )
        gate = BaseGate(name="stderr", command=["ruff", "check"], port=9004)
        client = TestClient(gate.app)
        resp = client.post("/run")
        data = resp.json()
        assert "error: something broke" in data["output"]
