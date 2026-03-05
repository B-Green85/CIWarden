"""Tests for gates.stress_gate — stress testing gate components."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from gates.stress_gate import (
    LoadPhase,
    LoadRunner,
    MockApiEmulator,
    PhaseMetrics,
    RateLimitState,
    RequestOutcome,
    StressConfig,
    StressGate,
    StressTestResult,
    _has_api_integrations,
    evaluate_stress_results,
)

# ── Data Model Tests ─────────────────────────────────────────


class TestLoadPhase:
    def test_enum_values(self) -> None:
        assert LoadPhase.NORMAL.value == "normal"
        assert LoadPhase.THRESHOLD.value == "threshold"
        assert LoadPhase.BREACH.value == "breach"

    def test_all_phases(self) -> None:
        assert len(LoadPhase) == 3


class TestStressConfig:
    def test_defaults(self) -> None:
        config = StressConfig()
        assert config.target_rate_limit == 100
        assert config.target_endpoint == "/api/v1/data"
        assert config.emulator_port == 9100
        assert config.normal_pct == 0.70
        assert config.threshold_pct == 1.00
        assert config.breach_pct == 1.20
        assert config.phase_duration_seconds == 10
        assert config.recovery_timeout_seconds == 30
        assert config.request_timeout_seconds == 5.0

    def test_custom_values(self) -> None:
        config = StressConfig(target_rate_limit=50, emulator_port=9200)
        assert config.target_rate_limit == 50
        assert config.emulator_port == 9200


class TestPhaseMetrics:
    def test_required_fields(self) -> None:
        metrics = PhaseMetrics(
            phase=LoadPhase.NORMAL,
            requests_sent=100,
            requests_succeeded=99,
            requests_failed=1,
            error_rate=0.01,
            timeout_count=0,
            timeout_rate=0.0,
            retry_storm_detected=False,
            cascade_detected=False,
            avg_latency_ms=5.0,
            p99_latency_ms=15.0,
        )
        assert metrics.phase == LoadPhase.NORMAL
        assert metrics.recovery_time_seconds is None

    def test_optional_recovery_time(self) -> None:
        metrics = PhaseMetrics(
            phase=LoadPhase.BREACH,
            requests_sent=100,
            requests_succeeded=80,
            requests_failed=20,
            error_rate=0.20,
            timeout_count=5,
            timeout_rate=0.05,
            retry_storm_detected=False,
            cascade_detected=False,
            avg_latency_ms=10.0,
            p99_latency_ms=50.0,
            recovery_time_seconds=2.5,
        )
        assert metrics.recovery_time_seconds == 2.5


class TestStressTestResult:
    def test_passing_result(self) -> None:
        result = StressTestResult(
            phases=[],
            overall_pass=True,
            summary="All stress phases passed",
        )
        assert result.overall_pass is True


class TestRateLimitState:
    def test_initial_state(self) -> None:
        state = RateLimitState(limit=100)
        assert state.limit == 100
        assert state.remaining == 100
        assert state.window_seconds == 60

    def test_reset_if_expired(self) -> None:
        state = RateLimitState(limit=100, window_seconds=0)
        state.remaining = 0
        state.reset_at = 0.0
        state.reset_if_expired()
        assert state.remaining == 100


class TestRequestOutcome:
    def test_success(self) -> None:
        outcome = RequestOutcome(
            success=True, status_code=200, latency_ms=5.0, was_retry=False
        )
        assert outcome.success is True
        assert outcome.status_code == 200

    def test_failure(self) -> None:
        outcome = RequestOutcome(
            success=False, status_code=429, latency_ms=10.0, was_retry=False
        )
        assert outcome.success is False


# ── MockApiEmulator Tests ────────────────────────────────────


class TestMockApiEmulator:
    def test_returns_200_under_limit(self) -> None:
        config = StressConfig(target_rate_limit=10, emulator_port=9101)
        emulator = MockApiEmulator(config)
        client = TestClient(emulator.app)
        resp = client.get("/api/v1/data")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_returns_rate_limit_headers(self) -> None:
        config = StressConfig(target_rate_limit=10, emulator_port=9102)
        emulator = MockApiEmulator(config)
        client = TestClient(emulator.app)
        resp = client.get("/api/v1/data")
        assert "x-ratelimit-limit" in resp.headers
        assert "x-ratelimit-remaining" in resp.headers
        assert "x-ratelimit-reset" in resp.headers

    def test_returns_429_when_exhausted(self) -> None:
        config = StressConfig(target_rate_limit=2, emulator_port=9103)
        emulator = MockApiEmulator(config)
        client = TestClient(emulator.app)
        client.get("/api/v1/data")
        client.get("/api/v1/data")
        resp = client.get("/api/v1/data")
        assert resp.status_code == 429
        assert "retry-after" in resp.headers

    def test_post_endpoint(self) -> None:
        config = StressConfig(target_rate_limit=10, emulator_port=9104)
        emulator = MockApiEmulator(config)
        client = TestClient(emulator.app)
        resp = client.post("/api/v1/data")
        assert resp.status_code == 200

    def test_remaining_decrements(self) -> None:
        config = StressConfig(target_rate_limit=5, emulator_port=9105)
        emulator = MockApiEmulator(config)
        client = TestClient(emulator.app)
        resp1 = client.get("/api/v1/data")
        resp2 = client.get("/api/v1/data")
        remaining1 = int(resp1.headers["x-ratelimit-remaining"])
        remaining2 = int(resp2.headers["x-ratelimit-remaining"])
        assert remaining2 < remaining1


# ── LoadRunner Tests ─────────────────────────────────────────


class TestLoadRunnerSendRequest:
    @pytest.mark.asyncio
    async def test_successful_request(self) -> None:
        config = StressConfig(emulator_port=9110)
        runner = LoadRunner(config)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)
        outcome = await runner._send_request(mock_client)
        assert outcome.success is True
        assert outcome.status_code == 200

    @pytest.mark.asyncio
    async def test_timeout_request(self) -> None:
        config = StressConfig(emulator_port=9111)
        runner = LoadRunner(config)
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
        outcome = await runner._send_request(mock_client)
        assert outcome.success is False
        assert outcome.status_code == 0

    @pytest.mark.asyncio
    async def test_429_request(self) -> None:
        config = StressConfig(emulator_port=9112)
        runner = LoadRunner(config)
        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)
        outcome = await runner._send_request(mock_client)
        assert outcome.success is False
        assert outcome.status_code == 429


# ── Evaluation Tests ─────────────────────────────────────────


def _make_metrics(
    phase: LoadPhase,
    error_rate: float = 0.0,
    cascade: bool = False,
    retry_storm: bool = False,
    recovery: float | None = None,
) -> PhaseMetrics:
    return PhaseMetrics(
        phase=phase,
        requests_sent=100,
        requests_succeeded=int(100 * (1 - error_rate)),
        requests_failed=int(100 * error_rate),
        error_rate=error_rate,
        timeout_count=0,
        timeout_rate=0.0,
        retry_storm_detected=retry_storm,
        cascade_detected=cascade,
        avg_latency_ms=5.0,
        p99_latency_ms=15.0,
        recovery_time_seconds=recovery,
    )


class TestEvaluateStressResults:
    def test_all_pass(self) -> None:
        config = StressConfig()
        phases = [
            _make_metrics(LoadPhase.NORMAL, error_rate=0.005),
            _make_metrics(LoadPhase.THRESHOLD, error_rate=0.03),
            _make_metrics(LoadPhase.BREACH, error_rate=0.10, recovery=5.0),
        ]
        passed, summary = evaluate_stress_results(phases, config)
        assert passed is True
        assert summary == "All stress phases passed"

    def test_normal_high_error_rate(self) -> None:
        config = StressConfig()
        phases = [
            _make_metrics(LoadPhase.NORMAL, error_rate=0.02),
            _make_metrics(LoadPhase.THRESHOLD),
            _make_metrics(LoadPhase.BREACH, recovery=1.0),
        ]
        passed, summary = evaluate_stress_results(phases, config)
        assert passed is False
        assert "NORMAL" in summary

    def test_threshold_high_error_rate(self) -> None:
        config = StressConfig()
        phases = [
            _make_metrics(LoadPhase.NORMAL),
            _make_metrics(LoadPhase.THRESHOLD, error_rate=0.06),
            _make_metrics(LoadPhase.BREACH, recovery=1.0),
        ]
        passed, summary = evaluate_stress_results(phases, config)
        assert passed is False
        assert "THRESHOLD" in summary

    def test_threshold_cascade(self) -> None:
        config = StressConfig()
        phases = [
            _make_metrics(LoadPhase.NORMAL),
            _make_metrics(LoadPhase.THRESHOLD, cascade=True),
            _make_metrics(LoadPhase.BREACH, recovery=1.0),
        ]
        passed, summary = evaluate_stress_results(phases, config)
        assert passed is False
        assert "cascade" in summary

    def test_breach_retry_storm(self) -> None:
        config = StressConfig()
        phases = [
            _make_metrics(LoadPhase.NORMAL),
            _make_metrics(LoadPhase.THRESHOLD),
            _make_metrics(LoadPhase.BREACH, retry_storm=True, recovery=1.0),
        ]
        passed, summary = evaluate_stress_results(phases, config)
        assert passed is False
        assert "retry storm" in summary

    def test_breach_slow_recovery(self) -> None:
        config = StressConfig(recovery_timeout_seconds=30)
        phases = [
            _make_metrics(LoadPhase.NORMAL),
            _make_metrics(LoadPhase.THRESHOLD),
            _make_metrics(LoadPhase.BREACH, recovery=35.0),
        ]
        passed, summary = evaluate_stress_results(phases, config)
        assert passed is False
        assert "recovery" in summary


# ── StressGate Integration Tests ─────────────────────────────


class TestStressGate:
    @pytest.fixture()
    def stress_gate(self) -> StressGate:
        return StressGate(
            name="stress",
            port=8007,
            config=StressConfig(),
        )

    @pytest.fixture()
    def client(self, stress_gate: StressGate) -> TestClient:
        return TestClient(stress_gate.app)

    def test_health_endpoint(self, client: TestClient) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["gate"] == "stress"
        assert data["status"] == "ready"
        assert data["port"] == 8007

    def test_run_skips_when_no_api_integrations(
        self, stress_gate: StressGate, client: TestClient
    ) -> None:
        with patch("gates.stress_gate._has_api_integrations", return_value=False):
            resp = client.post("/run")
        assert resp.status_code == 200
        data = resp.json()
        assert data["gate"] == "stress"
        assert data["status"] == "pass"
        assert data["exit_code"] == 0
        assert "No API integrations detected" in data["output"]

    def test_run_returns_gate_result_on_pass(
        self, stress_gate: StressGate, client: TestClient
    ) -> None:
        mock_result = StressTestResult(
            phases=[
                _make_metrics(LoadPhase.NORMAL, error_rate=0.005),
                _make_metrics(LoadPhase.THRESHOLD, error_rate=0.03),
                _make_metrics(LoadPhase.BREACH, recovery=2.0),
            ],
            overall_pass=True,
            summary="All stress phases passed",
        )
        with patch("gates.stress_gate._has_api_integrations", return_value=True), \
             patch.object(stress_gate, "_execute_stress_test", return_value=mock_result):
            resp = client.post("/run")
        assert resp.status_code == 200
        data = resp.json()
        assert data["gate"] == "stress"
        assert data["status"] == "pass"
        assert data["exit_code"] == 0
        assert "[STRESS GATE] PASS" in data["output"]

    def test_run_returns_gate_result_on_fail(
        self, stress_gate: StressGate, client: TestClient
    ) -> None:
        mock_result = StressTestResult(
            phases=[
                _make_metrics(LoadPhase.NORMAL, error_rate=0.05),
                _make_metrics(LoadPhase.THRESHOLD),
                _make_metrics(LoadPhase.BREACH, recovery=1.0),
            ],
            overall_pass=False,
            summary="NORMAL: error rate 5.00% >= 1%",
        )
        with patch("gates.stress_gate._has_api_integrations", return_value=True), \
             patch.object(stress_gate, "_execute_stress_test", return_value=mock_result):
            resp = client.post("/run")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "fail"
        assert data["exit_code"] == 1

    def test_run_handles_internal_error(
        self, stress_gate: StressGate, client: TestClient
    ) -> None:
        with patch("gates.stress_gate._has_api_integrations", return_value=True), \
             patch.object(
                stress_gate,
                "_execute_stress_test",
                side_effect=RuntimeError("emulator crash"),
             ):
            resp = client.post("/run")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "fail"
        assert data["exit_code"] == 2
        assert "emulator crash" in data["output"]


# ── API Integration Detection Tests ─────────────────────────


class TestHasApiIntegrations:
    def test_detects_httpx_import(self, tmp_path: object) -> None:
        import pathlib

        p = pathlib.Path(str(tmp_path)) / "client.py"
        p.write_text("import httpx\n")
        assert _has_api_integrations(str(tmp_path)) is True

    def test_detects_requests_from_import(self, tmp_path: object) -> None:
        import pathlib

        p = pathlib.Path(str(tmp_path)) / "api.py"
        p.write_text("from requests import Session\n")
        assert _has_api_integrations(str(tmp_path)) is True

    def test_no_api_imports(self, tmp_path: object) -> None:
        import pathlib

        p = pathlib.Path(str(tmp_path)) / "utils.py"
        p.write_text("import os\nimport json\n")
        assert _has_api_integrations(str(tmp_path)) is False

    def test_empty_directory(self, tmp_path: object) -> None:
        assert _has_api_integrations(str(tmp_path)) is False

    def test_excludes_tests_directory(self, tmp_path: object) -> None:
        import pathlib

        tests_dir = pathlib.Path(str(tmp_path)) / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_api.py").write_text("import httpx\n")
        assert _has_api_integrations(str(tmp_path)) is False

    def test_excludes_infrastructure_directories(self, tmp_path: object) -> None:
        import pathlib

        root = pathlib.Path(str(tmp_path))
        for infra_dir in ("orchestrator", "gates", "hooks", "scripts", "memory"):
            d = root / infra_dir
            d.mkdir()
            (d / "service.py").write_text("import httpx\n")
        assert _has_api_integrations(str(tmp_path)) is False

    def test_detects_app_code_alongside_infra(self, tmp_path: object) -> None:
        import pathlib

        root = pathlib.Path(str(tmp_path))
        (root / "orchestrator").mkdir()
        (root / "orchestrator" / "orch.py").write_text("import httpx\n")
        (root / "myapp").mkdir()
        (root / "myapp" / "client.py").write_text("import requests\n")
        assert _has_api_integrations(str(tmp_path)) is True
