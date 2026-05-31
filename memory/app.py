"""FastAPI app for the Generational Memory Gate — port 8006."""

from __future__ import annotations

import os
import time

from fastapi import FastAPI

from gates.base_gate import GateResult, GateStatus
from memory.contract_store import BaseContractStore, ContractStore, VectorContractStore
from memory.memory_gate import MemoryGate
from memory.schema_capture import SchemaCaptureClient, _get_repo_name

app = FastAPI(title="CI Gate: memory")


def get_store() -> BaseContractStore:
    """Select the contract store backend from CDMAD_STORE (json|vdb)."""
    if os.environ.get("CDMAD_STORE") == "vdb":
        return VectorContractStore(
            repo_name=os.environ.get("CDMAD_REPO_NAME") or _get_repo_name(),
            vdb_root=os.environ.get("CDMAD_VDB_PATH", ".cdmad/vdb"),
        )
    return ContractStore(root=os.environ.get("CDMAD_ROOT", ".cdmad"))


def get_client() -> SchemaCaptureClient:
    """Build the static (LLM-free) extraction client."""
    return SchemaCaptureClient(
        schema_path=os.environ.get("CDMAD_SCHEMA_PATH", ".cdmad/session_schema.json"),
        repo_name=os.environ.get("CDMAD_REPO_NAME"),
    )


@app.get("/health")
async def health() -> dict[str, str | int]:
    return {"gate": "memory", "status": "ready", "port": 8006}


@app.post("/run", response_model=GateResult)
async def run() -> GateResult:
    start = time.time()
    try:
        gate = MemoryGate(
            llm_client=get_client(),
            store=get_store(),
            repo_name=os.environ.get("CDMAD_REPO_NAME"),
            agent_id=os.environ.get("CDMAD_AGENT_ID"),
        )
        return await gate.run()
    except Exception as e:  # noqa: BLE001
        duration = int((time.time() - start) * 1000)
        return GateResult(
            gate="memory",
            status=GateStatus.FAIL,
            output=f"Memory gate error: {e}",
            exit_code=1,
            duration_ms=duration,
        )
