"""Tests for orchestrator.orchestrator — coordination engine."""

from __future__ import annotations

import os
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orchestrator.orchestrator import (
    CommitPayload,
    app,
    generate_token,
    save_result,
    save_token,
)


@pytest.fixture(autouse=True)
def _use_temp_db(tmp_path: Path) -> Iterator[None]:
    """Redirect DB to a temp file for test isolation."""
    # Patch at module level so all functions use temp DB
    import orchestrator.orchestrator as orch

    original = orch.DB_PATH
    orch.DB_PATH = os.path.join(str(tmp_path), "test_gate_results.db")
    init_db_at(orch.DB_PATH)
    yield
    orch.DB_PATH = original


def init_db_at(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gate_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            commit_sha TEXT, agent_id TEXT, branch TEXT,
            gate_name TEXT, status TEXT, output TEXT,
            exit_code INTEGER, duration_ms INTEGER, timestamp REAL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS merge_tokens (
            token TEXT PRIMARY KEY, commit_sha TEXT,
            issued_at REAL, used INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


class TestGenerateToken:
    def test_returns_24_char_hex(self) -> None:
        token = generate_token("abc123")
        assert len(token) == 24
        assert all(c in "0123456789abcdef" for c in token)

    def test_different_shas_produce_different_tokens(self) -> None:
        t1 = generate_token("sha1")
        t2 = generate_token("sha2")
        assert t1 != t2


class TestSaveResult:
    def test_inserts_row(self, _use_temp_db: None) -> None:
        import orchestrator.orchestrator as orch

        save_result("sha1", "agent1", "main", "lint", {
            "status": "pass", "output": "ok",
            "exit_code": 0, "duration_ms": 100,
        })
        conn = sqlite3.connect(orch.DB_PATH)
        rows = conn.execute("SELECT * FROM gate_runs").fetchall()
        conn.close()
        assert len(rows) == 1


class TestSaveToken:
    def test_inserts_token(self, _use_temp_db: None) -> None:
        import orchestrator.orchestrator as orch

        save_token("tok123", "sha1")
        conn = sqlite3.connect(orch.DB_PATH)
        rows = conn.execute("SELECT * FROM merge_tokens").fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][0] == "tok123"


class TestStatusEndpoint:
    def test_returns_orchestrator_online(self, client: TestClient) -> None:
        resp = client.get("/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["orchestrator"] == "online"
        assert "gates" in data


class TestResultsEndpoint:
    def test_404_for_unknown_sha(self, client: TestClient) -> None:
        resp = client.get("/results/unknown_sha")
        assert resp.status_code == 404


class TestVerifyEndpoint:
    def test_404_for_unknown_token(self, client: TestClient) -> None:
        resp = client.get("/verify/nonexistent_token")
        assert resp.status_code == 404


class TestCommitPayload:
    def test_valid_payload(self) -> None:
        payload = CommitPayload(
            agent_id="test-agent",
            branch="main",
            commit_sha="abc123def456",
        )
        assert payload.agent_id == "test-agent"
