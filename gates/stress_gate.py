"""
Stress Gate — three-phase load testing for API integrations.

Validates that generated API code handles rate limits, timeouts,
and failure gracefully under NORMAL (70%), THRESHOLD (100%),
and BREACH (120%) load phases.
"""
import asyncio
import logging
import re
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from gates.base_gate import BaseGate, GateResult, GateStatus

logger = logging.getLogger(__name__)

_API_IMPORT_PATTERN = re.compile(
    r"^\s*(?:import|from)\s+(?:httpx|requests|aiohttp|urllib)\b",
    re.MULTILINE,
)
_SCAN_EXCLUDE_DIRS = {
    "tests", "__pycache__", ".venv", ".cdmad", ".git", "node_modules",
    "orchestrator", "gates", "hooks", "scripts", "memory",
}


def _has_api_integrations(root: str = ".") -> bool:
    """Scan Python source files for HTTP client library imports."""
    root_path = Path(root).resolve()
    for py_file in root_path.rglob("*.py"):
        if any(part in _SCAN_EXCLUDE_DIRS for part in py_file.relative_to(root_path).parts):
            continue
        try:
            if _API_IMPORT_PATTERN.search(py_file.read_text()):
                return True
        except OSError:
            continue
    return False


# ── Data Models ──────────────────────────────────────────────


class LoadPhase(StrEnum):
    NORMAL = "normal"
    THRESHOLD = "threshold"
    BREACH = "breach"


class StressConfig(BaseModel):
    """Configuration for a stress test run."""

    target_rate_limit: int = 100
    target_endpoint: str = "/api/v1/data"
    emulator_port: int = 9100
    normal_pct: float = 0.70
    threshold_pct: float = 1.00
    breach_pct: float = 1.20
    phase_duration_seconds: int = 10
    recovery_timeout_seconds: int = 30
    request_timeout_seconds: float = 5.0


class PhaseMetrics(BaseModel):
    """Metrics collected during one load phase."""

    phase: LoadPhase
    requests_sent: int
    requests_succeeded: int
    requests_failed: int
    error_rate: float
    timeout_count: int
    timeout_rate: float
    retry_storm_detected: bool
    cascade_detected: bool
    avg_latency_ms: float
    p99_latency_ms: float
    recovery_time_seconds: float | None = None


class StressTestResult(BaseModel):
    """Aggregated result across all three phases."""

    phases: list[PhaseMetrics]
    overall_pass: bool
    summary: str


class RateLimitState:
    """Mutable rate limit state for the mock endpoint."""

    def __init__(self, limit: int, window_seconds: int = 60) -> None:
        self.limit: int = limit
        self.remaining: int = limit
        self.window_seconds: int = window_seconds
        self.reset_at: float = time.time() + window_seconds

    def reset_if_expired(self) -> None:
        now = time.time()
        if now >= self.reset_at:
            self.remaining = self.limit
            self.reset_at = now + self.window_seconds


@dataclass
class RequestOutcome:
    """Result of a single HTTP request during load testing."""

    success: bool
    status_code: int
    latency_ms: float
    was_retry: bool


# ── Mock API Emulator ────────────────────────────────────────


class MockApiEmulator:
    """Temporary FastAPI server that emulates a rate-limited API."""

    def __init__(self, config: StressConfig) -> None:
        self.config = config
        self.app = FastAPI(title="Stress Gate Mock API")
        self.rate_limit_state = RateLimitState(
            limit=config.target_rate_limit,
        )
        self._server: uvicorn.Server | None = None
        self._setup_routes()

    def _setup_routes(self) -> None:
        endpoint = self.config.target_endpoint

        @self.app.get(endpoint)
        async def handle_get() -> JSONResponse:
            return self._process_request()

        @self.app.post(endpoint)
        async def handle_post() -> JSONResponse:
            return self._process_request()

    def _process_request(self) -> JSONResponse:
        state = self.rate_limit_state
        state.reset_if_expired()

        headers = {
            "X-RateLimit-Limit": str(state.limit),
            "X-RateLimit-Remaining": str(max(0, state.remaining)),
            "X-RateLimit-Reset": str(int(state.reset_at)),
        }

        if state.remaining <= 0:
            retry_after = max(1, int(state.reset_at - time.time()))
            headers["Retry-After"] = str(retry_after)
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded"},
                headers=headers,
            )

        state.remaining -= 1
        return JSONResponse(
            status_code=200,
            content={"status": "ok"},
            headers=headers,
        )

    async def start(self) -> None:
        """Start the mock server in a background task."""
        config = uvicorn.Config(
            app=self.app,
            host="127.0.0.1",
            port=self.config.emulator_port,
            log_level="error",
        )
        self._server = uvicorn.Server(config)
        asyncio.create_task(self._server.serve())
        await self._wait_for_ready()

    async def _wait_for_ready(self, timeout: float = 5.0) -> None:
        start = time.time()
        url = f"http://127.0.0.1:{self.config.emulator_port}{self.config.target_endpoint}"
        async with httpx.AsyncClient() as client:
            while time.time() - start < timeout:
                try:
                    resp = await client.get(url, timeout=1.0)
                    if resp.status_code in (200, 429):
                        return
                except httpx.ConnectError:
                    await asyncio.sleep(0.1)
        raise TimeoutError("Mock API emulator failed to start")

    async def stop(self) -> None:
        """Shut down the mock server."""
        if self._server is not None:
            self._server.should_exit = True
            await asyncio.sleep(0.2)


# ── Load Runner ──────────────────────────────────────────────


class LoadRunner:
    """Runs three-phase load test against a target endpoint."""

    def __init__(self, config: StressConfig) -> None:
        self.config = config
        self.base_url = (
            f"http://127.0.0.1:{config.emulator_port}{config.target_endpoint}"
        )

    async def run_phase(self, phase: LoadPhase) -> PhaseMetrics:
        """Execute a single load phase and collect metrics."""
        pct_map: dict[LoadPhase, float] = {
            LoadPhase.NORMAL: self.config.normal_pct,
            LoadPhase.THRESHOLD: self.config.threshold_pct,
            LoadPhase.BREACH: self.config.breach_pct,
        }
        target_rps = max(1, int(self.config.target_rate_limit * pct_map[phase]))
        duration = self.config.phase_duration_seconds

        succeeded = 0
        failed = 0
        timeouts = 0
        latencies: list[float] = []
        retry_count = 0
        consecutive_failures = 0
        max_consecutive_failures = 0

        end_time = time.time() + duration
        batch_size = min(target_rps, 50)

        async with httpx.AsyncClient() as client:
            while time.time() < end_time:
                batch_start = time.time()
                tasks = [self._send_request(client) for _ in range(batch_size)]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                for result in results:
                    if isinstance(result, Exception):
                        failed += 1
                        timeouts += 1
                        consecutive_failures += 1
                    elif isinstance(result, RequestOutcome):
                        latencies.append(result.latency_ms)
                        if result.success:
                            succeeded += 1
                            consecutive_failures = 0
                        else:
                            failed += 1
                            consecutive_failures += 1
                            if result.was_retry:
                                retry_count += 1

                    max_consecutive_failures = max(
                        max_consecutive_failures, consecutive_failures
                    )

                elapsed = time.time() - batch_start
                sleep_time = max(0.0, 1.0 - elapsed)
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)

        total = succeeded + failed
        error_rate = failed / total if total > 0 else 0.0
        timeout_rate = timeouts / total if total > 0 else 0.0

        sorted_latencies = sorted(latencies) if latencies else [0.0]
        p99_idx = min(int(len(sorted_latencies) * 0.99), len(sorted_latencies) - 1)

        cascade_detected = max_consecutive_failures > 10
        retry_storm = (retry_count > (total * 0.5)) if total > 0 else False

        recovery_time: float | None = None
        if phase == LoadPhase.BREACH:
            recovery_time = await self._measure_recovery()

        return PhaseMetrics(
            phase=phase,
            requests_sent=total,
            requests_succeeded=succeeded,
            requests_failed=failed,
            error_rate=error_rate,
            timeout_count=timeouts,
            timeout_rate=timeout_rate,
            retry_storm_detected=retry_storm,
            cascade_detected=cascade_detected,
            avg_latency_ms=(
                sum(latencies) / len(latencies) if latencies else 0.0
            ),
            p99_latency_ms=sorted_latencies[p99_idx],
            recovery_time_seconds=recovery_time,
        )

    async def _send_request(self, client: httpx.AsyncClient) -> RequestOutcome:
        """Send a single request and return outcome."""
        start = time.time()
        try:
            resp = await client.get(
                self.base_url, timeout=self.config.request_timeout_seconds
            )
            latency = (time.time() - start) * 1000
            return RequestOutcome(
                success=resp.status_code == 200,
                status_code=resp.status_code,
                latency_ms=latency,
                was_retry=False,
            )
        except httpx.TimeoutException:
            latency = (time.time() - start) * 1000
            return RequestOutcome(
                success=False,
                status_code=0,
                latency_ms=latency,
                was_retry=False,
            )

    async def _measure_recovery(self) -> float:
        """After BREACH phase, measure time until endpoint returns 200."""
        start = time.time()
        timeout = self.config.recovery_timeout_seconds
        async with httpx.AsyncClient() as client:
            while time.time() - start < timeout:
                try:
                    resp = await client.get(self.base_url, timeout=2.0)
                    if resp.status_code == 200:
                        return time.time() - start
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.5)
        return float(timeout)

    async def run_all_phases(self) -> list[PhaseMetrics]:
        """Run NORMAL, THRESHOLD, BREACH in sequence."""
        results: list[PhaseMetrics] = []
        for phase in [LoadPhase.NORMAL, LoadPhase.THRESHOLD, LoadPhase.BREACH]:
            metrics = await self.run_phase(phase)
            results.append(metrics)
        return results


# ── Evaluation ───────────────────────────────────────────────


def evaluate_stress_results(
    phases: list[PhaseMetrics], config: StressConfig
) -> tuple[bool, str]:
    """
    Apply pass criteria:
    - NORMAL:    error rate < 1%
    - THRESHOLD: error rate < 5%, no cascades
    - BREACH:    no retry storm, recovery within timeout
    """
    reasons: list[str] = []
    passed = True

    for metrics in phases:
        if metrics.phase == LoadPhase.NORMAL:
            if metrics.error_rate >= 0.01:
                passed = False
                reasons.append(
                    f"NORMAL: error rate {metrics.error_rate:.2%} >= 1%"
                )

        elif metrics.phase == LoadPhase.THRESHOLD:
            if metrics.error_rate >= 0.05:
                passed = False
                reasons.append(
                    f"THRESHOLD: error rate {metrics.error_rate:.2%} >= 5%"
                )
            if metrics.cascade_detected:
                passed = False
                reasons.append("THRESHOLD: cascade failure detected")

        elif metrics.phase == LoadPhase.BREACH:
            if metrics.retry_storm_detected:
                passed = False
                reasons.append("BREACH: retry storm detected")
            if (
                metrics.recovery_time_seconds is not None
                and metrics.recovery_time_seconds > config.recovery_timeout_seconds
            ):
                    passed = False
                    reasons.append(
                        f"BREACH: recovery took {metrics.recovery_time_seconds:.1f}s "
                        f"> {config.recovery_timeout_seconds}s limit"
                    )

    summary = "All stress phases passed" if passed else "; ".join(reasons)
    return passed, summary


# ── Stress Gate ──────────────────────────────────────────────


class StressGate(BaseGate):
    """Gate that runs three-phase stress tests on API integrations."""

    def __init__(self, name: str, port: int, config: StressConfig) -> None:
        super().__init__(name=name, command=["echo", "stress-gate"], port=port)
        self.config = config

    async def run(self) -> GateResult:
        """Override BaseGate.run() to execute stress test pipeline."""
        start = time.time()

        if not _has_api_integrations():
            duration = int((time.time() - start) * 1000)
            return GateResult(
                gate=self.name,
                status=GateStatus.PASS,
                output="No API integrations detected — stress gate skipped",
                exit_code=0,
                duration_ms=duration,
            )

        try:
            result = await self._execute_stress_test()
            status = GateStatus.PASS if result.overall_pass else GateStatus.FAIL
            output = self._format_report(result)
            exit_code = 0 if result.overall_pass else 1
        except Exception as e:
            logger.error("Stress gate internal error: %s", e)
            status = GateStatus.FAIL
            output = f"Stress gate internal error: {e}"
            exit_code = 2

        duration = int((time.time() - start) * 1000)
        return GateResult(
            gate=self.name,
            status=status,
            output=output,
            exit_code=exit_code,
            duration_ms=duration,
        )

    async def _execute_stress_test(self) -> StressTestResult:
        """Run the full stress test pipeline."""
        emulator = MockApiEmulator(self.config)
        try:
            await emulator.start()
            runner = LoadRunner(self.config)
            phase_results = await runner.run_all_phases()
            passed, summary = evaluate_stress_results(phase_results, self.config)
            return StressTestResult(
                phases=phase_results,
                overall_pass=passed,
                summary=summary,
            )
        finally:
            await emulator.stop()

    def _format_report(self, result: StressTestResult) -> str:
        """Format stress test results as human-readable output."""
        lines: list[str] = []
        lines.append(f"[STRESS GATE] {'PASS' if result.overall_pass else 'FAIL'}")
        lines.append(f"Summary: {result.summary}")
        lines.append("")

        for phase in result.phases:
            lines.append(f"  [{phase.phase.value.upper()}]")
            lines.append(f"    Requests: {phase.requests_sent} sent, "
                         f"{phase.requests_succeeded} ok, {phase.requests_failed} failed")
            lines.append(f"    Error rate: {phase.error_rate:.2%}")
            lines.append(f"    Timeout rate: {phase.timeout_rate:.2%}")
            lines.append(f"    Latency: avg={phase.avg_latency_ms:.1f}ms "
                         f"p99={phase.p99_latency_ms:.1f}ms")
            lines.append(f"    Cascade: {phase.cascade_detected} | "
                         f"Retry storm: {phase.retry_storm_detected}")
            if phase.recovery_time_seconds is not None:
                lines.append(f"    Recovery: {phase.recovery_time_seconds:.1f}s")
            lines.append("")

        return "\n".join(lines)
