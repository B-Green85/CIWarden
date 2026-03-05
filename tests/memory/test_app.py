"""Tests for memory.app — FastAPI endpoint for the Memory Gate."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from gates.base_gate import GateResult, GateStatus
from memory.app import app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


class TestHealthEndpoint:
    def test_returns_ready(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["gate"] == "memory"
        assert data["status"] == "ready"
        assert data["port"] == 8006


class TestRunEndpoint:
    def test_run_returns_gate_result_on_success(self, client: TestClient) -> None:
        mock_result = GateResult(
            gate="memory",
            status=GateStatus.PASS,
            output="All clear",
            exit_code=0,
            duration_ms=100,
        )
        with patch("memory.app.get_llm_client"), \
             patch("memory.app.MemoryGate") as mock_gate_cls:
            mock_gate_instance = mock_gate_cls.return_value
            mock_gate_instance.run = AsyncMock(return_value=mock_result)

            resp = client.post("/run")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "pass"
            assert data["gate"] == "memory"

    def test_run_returns_fail_on_missing_api_key(self, client: TestClient) -> None:
        with patch.dict("os.environ", {}, clear=True), \
             patch("memory.app.get_llm_client", side_effect=RuntimeError("No API key")):
                resp = client.post("/run")
                assert resp.status_code == 200
                data = resp.json()
                assert data["status"] == "fail"
                assert "error" in data["output"].lower()

    def test_run_returns_fail_on_gate_error(self, client: TestClient) -> None:
        with patch("memory.app.get_llm_client"), \
             patch("memory.app.MemoryGate") as mock_gate_cls:
            mock_gate_instance = mock_gate_cls.return_value
            mock_gate_instance.run = AsyncMock(side_effect=RuntimeError("boom"))

            resp = client.post("/run")
            data = resp.json()
            assert data["status"] == "fail"
