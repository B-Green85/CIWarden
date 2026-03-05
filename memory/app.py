"""FastAPI app for the Generational Memory Gate — port 8006."""

from __future__ import annotations

import os
import time

from fastapi import FastAPI

from gates.base_gate import GateResult, GateStatus
from memory.contract_store import ContractStore
from memory.llm_client import AnthropicClient, LLMClient
from memory.memory_gate import MemoryGate

app = FastAPI(title="CI Gate: memory")


def get_llm_client() -> LLMClient:
    """Build LLM client from environment variables."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        msg = "ANTHROPIC_API_KEY environment variable required for memory gate"
        raise RuntimeError(msg)
    return AnthropicClient(api_key=api_key)


@app.get("/health")
async def health() -> dict[str, str | int]:
    return {"gate": "memory", "status": "ready", "port": 8006}


@app.post("/run", response_model=GateResult)
async def run() -> GateResult:
    start = time.time()
    try:
        client = get_llm_client()
        store = ContractStore()
        gate = MemoryGate(llm_client=client, store=store)
        return await gate.run()
    except Exception as e:
        duration = int((time.time() - start) * 1000)
        return GateResult(
            gate="memory",
            status=GateStatus.FAIL,
            output=f"Memory gate error: {e}",
            exit_code=1,
            duration_ms=duration,
        )
