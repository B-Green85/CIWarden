"""
CI Gate Orchestrator — the enforcement brain.
Receives agent commits, runs gate chain, issues or blocks merge tokens.

POST /commit  → run full gate chain
GET  /status  → current gate health
GET  /results/{sha} → results for a specific commit
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import sqlite3
import sys
import time
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from gates.gates import GATE_REGISTRY  # noqa: E402

app = FastAPI(title="CI Gate Orchestrator", version="1.0.0")

DB_PATH = os.path.join(os.path.dirname(__file__), "gate_results.db")

# ── Database setup ────────────────────────────────────────────
def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gate_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            commit_sha TEXT,
            agent_id TEXT,
            branch TEXT,
            gate_name TEXT,
            status TEXT,
            output TEXT,
            exit_code INTEGER,
            duration_ms INTEGER,
            timestamp REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS merge_tokens (
            token TEXT PRIMARY KEY,
            commit_sha TEXT,
            issued_at REAL,
            used INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

init_db()


# ── Models ────────────────────────────────────────────────────
class CommitPayload(BaseModel):
    agent_id: str
    branch: str
    commit_sha: str


class GateChainResult(BaseModel):
    agent_id: str
    commit_sha: str
    branch: str
    merge_token: str | None
    blocked_at: str | None
    reason: str | None
    total_duration_ms: int
    results: dict[str, Any]


# ── Helpers ───────────────────────────────────────────────────
def generate_token(sha: str) -> str:
    return hashlib.sha256(f"{sha}{time.time()}".encode()).hexdigest()[:24]


def save_result(
    commit_sha: str,
    agent_id: str,
    branch: str,
    gate_name: str,
    result: dict[str, Any],
) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO gate_runs
        (commit_sha, agent_id, branch, gate_name, status, output, exit_code, duration_ms, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        commit_sha, agent_id, branch, gate_name,
        result["status"], result["output"],
        result["exit_code"], result["duration_ms"],
        time.time()
    ))
    conn.commit()
    conn.close()


def save_token(token: str, commit_sha: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO merge_tokens (token, commit_sha, issued_at) VALUES (?, ?, ?)",
        (token, commit_sha, time.time())
    )
    conn.commit()
    conn.close()


async def call_gate(client: httpx.AsyncClient, name: str) -> dict[str, Any]:
    config = GATE_REGISTRY[name]
    url = f"http://localhost:{config['port']}/run"
    try:
        response = await client.post(url, timeout=120)
        return response.json()  # type: ignore[no-any-return]
    except Exception as e:
        return {
            "gate": name,
            "status": "fail",
            "output": f"Gate service unreachable: {e}",
            "exit_code": -1,
            "duration_ms": 0,
        }


def _gate_order(name: str) -> int:
    """Return the order key for a gate, defaulting to 99."""
    order = GATE_REGISTRY[name].get("order", 99)
    return int(order)


# ── Routes ────────────────────────────────────────────────────
@app.post("/commit", response_model=GateChainResult)
async def handle_commit(payload: CommitPayload) -> GateChainResult:
    start = time.time()
    results: dict[str, Any] = {}

    print(f"\n[ORCHESTRATOR] Commit received from agent '{payload.agent_id}'")
    print(f"  Branch: {payload.branch} | SHA: {payload.commit_sha[:8]}")

    parallel_gates = [k for k, v in GATE_REGISTRY.items() if v["phase"] == "parallel"]
    sequential_gates = sorted(
        [k for k, v in GATE_REGISTRY.items() if v["phase"] == "sequential"],
        key=_gate_order,
    )

    # Phase 1 — parallel gates
    print(f"[PHASE 1] Running parallel gates: {parallel_gates}")
    async with httpx.AsyncClient() as client:
        tasks = [call_gate(client, name) for name in parallel_gates]
        responses = await asyncio.gather(*tasks)

    for name, result in zip(parallel_gates, responses, strict=True):
        results[name] = result
        save_result(payload.commit_sha, payload.agent_id, payload.branch, name, result)
        status_icon = "✓" if result["status"] == "pass" else "✗"
        print(f"  {status_icon} {name}: {result['status'].upper()} ({result['duration_ms']}ms)")

        if result["status"] == "fail":
            total = int((time.time() - start) * 1000)
            print(f"[BLOCKED] Gate chain halted at '{name}'")
            return GateChainResult(
                agent_id=payload.agent_id,
                commit_sha=payload.commit_sha,
                branch=payload.branch,
                merge_token=None,
                blocked_at=name,
                reason=result["output"],
                total_duration_ms=total,
                results=results,
            )

    # Phase 2 — sequential gates
    print(f"[PHASE 2] Running sequential gates: {sequential_gates}")
    async with httpx.AsyncClient() as client:
        for name in sequential_gates:
            result = await call_gate(client, name)
            results[name] = result
            save_result(payload.commit_sha, payload.agent_id, payload.branch, name, result)
            status_icon = "✓" if result["status"] == "pass" else "✗"
            print(f"  {status_icon} {name}: {result['status'].upper()} ({result['duration_ms']}ms)")

            if result["status"] == "fail":
                total = int((time.time() - start) * 1000)
                print(f"[BLOCKED] Gate chain halted at '{name}'")
                return GateChainResult(
                    agent_id=payload.agent_id,
                    commit_sha=payload.commit_sha,
                    branch=payload.branch,
                    merge_token=None,
                    blocked_at=name,
                    reason=result["output"],
                    total_duration_ms=total,
                    results=results,
                )

    # All gates passed
    token = generate_token(payload.commit_sha)
    save_token(token, payload.commit_sha)
    total = int((time.time() - start) * 1000)
    print(f"[PASS] All gates cleared. Merge token issued: {token}")

    return GateChainResult(
        agent_id=payload.agent_id,
        commit_sha=payload.commit_sha,
        branch=payload.branch,
        merge_token=token,
        blocked_at=None,
        reason=None,
        total_duration_ms=total,
        results=results,
    )


@app.get("/status")
async def get_status() -> dict[str, Any]:
    """Check health of all gate services."""
    statuses: dict[str, Any] = {}
    async with httpx.AsyncClient() as client:
        for name, config in GATE_REGISTRY.items():
            try:
                r = await client.get(f"http://localhost:{config['port']}/health", timeout=3)
                statuses[name] = r.json()
            except Exception:
                statuses[name] = {"gate": name, "status": "offline", "port": config["port"]}
    return {"orchestrator": "online", "gates": statuses}


@app.get("/results/{commit_sha}")
async def get_results(commit_sha: str) -> dict[str, Any]:
    """Retrieve gate results for a specific commit SHA."""
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT gate_name, status, output, exit_code, duration_ms, timestamp FROM gate_runs WHERE commit_sha = ?",
        (commit_sha,)
    ).fetchall()
    conn.close()

    if not rows:
        raise HTTPException(404, f"No results found for SHA: {commit_sha}")

    return {
        "commit_sha": commit_sha,
        "gates": [
            {
                "gate": r[0], "status": r[1], "output": r[2],
                "exit_code": r[3], "duration_ms": r[4], "timestamp": r[5],
            }
            for r in rows
        ],
    }


@app.get("/verify/{token}")
async def verify_token(token: str) -> dict[str, Any]:
    """Verify a merge token is valid and unused."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT commit_sha, issued_at, used FROM merge_tokens WHERE token = ?",
        (token,)
    ).fetchone()
    conn.close()

    if not row:
        raise HTTPException(404, "Token not found")
    if row[2]:
        raise HTTPException(400, "Token already used")

    return {"valid": True, "commit_sha": row[0], "issued_at": row[1]}


if __name__ == "__main__":
    import uvicorn
    print("[ORCHESTRATOR] Starting on port 8000")
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")  # nosec B104
