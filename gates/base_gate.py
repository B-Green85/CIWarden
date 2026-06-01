import subprocess
import time
from enum import StrEnum

from fastapi import FastAPI
from pydantic import BaseModel


class GateStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    RUNNING = "running"


class GateResult(BaseModel):
    gate: str
    status: GateStatus
    output: str
    exit_code: int
    duration_ms: int


class BaseGate:
    def __init__(self, name: str, command: list[str], port: int):
        self.name = name
        self.command = command
        self.port = port
        self.app = FastAPI(title=f"CIWarden: {name}")
        self.app.post("/run", response_model=GateResult)(self.run)
        self.app.get("/health")(self.health)

    async def health(self) -> dict[str, str | int]:
        return {"gate": self.name, "status": "ready", "port": self.port}

    async def run(self) -> GateResult:
        start = time.time()
        try:
            result = subprocess.run(
                self.command,
                capture_output=True,
                text=True,
                cwd="."
            )
            duration = int((time.time() - start) * 1000)
            status = GateStatus.PASS if result.returncode == 0 else GateStatus.FAIL
            output = result.stdout + result.stderr
        except FileNotFoundError as e:
            duration = int((time.time() - start) * 1000)
            status = GateStatus.FAIL
            output = f"Tool not found: {e}. Install required dependencies."
            result = type("r", (), {"returncode": 127})()

        return GateResult(
            gate=self.name,
            status=status,
            output=output,
            exit_code=result.returncode,
            duration_ms=duration
        )
