"""Tests for orchestrator.auth — API key authentication."""
from __future__ import annotations

import os
import sqlite3
from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def auth_db(tmp_path: Path) -> str:
    """Create a temp auth DB and return its path."""
    from orchestrator.auth import init_auth_db

    db_path = str(tmp_path / "test_auth.db")
    init_auth_db(db_path)
    return db_path


@pytest.fixture(autouse=True)
def _use_temp_dbs(tmp_path: Path) -> Iterator[None]:
    """Redirect both DBs to temp files for test isolation."""
    import orchestrator.auth as auth_mod
    import orchestrator.orchestrator as orch

    orig_auth_db = auth_mod.AUTH_DB_PATH
    orig_gate_db = orch.DB_PATH

    auth_db_path = str(tmp_path / "test_auth.db")
    gate_db_path = str(tmp_path / "test_gate_results.db")

    auth_mod.AUTH_DB_PATH = auth_db_path
    orch.DB_PATH = gate_db_path

    auth_mod.init_auth_db(auth_db_path)
    _init_gate_db(gate_db_path)

    yield

    auth_mod.AUTH_DB_PATH = orig_auth_db
    orch.DB_PATH = orig_gate_db


def _init_gate_db(path: str) -> None:
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
    from orchestrator.orchestrator import app

    return TestClient(app)


PAYLOAD = {
    "agent_id": "test-agent",
    "branch": "main",
    "commit_sha": "abc123def456",
}


class TestAuthModule:
    def test_generate_and_validate_key(self, auth_db: str) -> None:
        from orchestrator.auth import generate_api_key, validate_key

        key = generate_api_key("test", auth_db)
        assert len(key) == 64  # 32 bytes hex
        assert validate_key(key, auth_db)

    def test_invalid_key_rejected(self, auth_db: str) -> None:
        from orchestrator.auth import validate_key

        assert not validate_key("bogus-key", auth_db)

    def test_ensure_master_key_creates_once(self, auth_db: str) -> None:
        from orchestrator.auth import ensure_master_key

        key1 = ensure_master_key(auth_db)
        assert key1 is not None
        key2 = ensure_master_key(auth_db)
        assert key2 is None  # already exists

    def test_is_local_dev_mode(self) -> None:
        from orchestrator.auth import is_local_dev_mode

        with patch.dict(os.environ, {"CDMAE_LOCAL_DEV": "1"}):
            assert is_local_dev_mode()
        with patch.dict(os.environ, {"CDMAE_LOCAL_DEV": "0"}):
            assert not is_local_dev_mode()


class TestAuthMiddleware:
    def test_missing_token_returns_401(self, client: TestClient) -> None:
        with patch.dict(os.environ, {"CDMAE_LOCAL_DEV": "0"}, clear=False):
            resp = client.post("/commit", json=PAYLOAD)
        assert resp.status_code == 401
        assert "Missing" in resp.json()["detail"]

    def test_invalid_token_returns_401(self, client: TestClient) -> None:
        with patch.dict(os.environ, {"CDMAE_LOCAL_DEV": "0"}, clear=False):
            resp = client.post(
                "/commit",
                json=PAYLOAD,
                headers={"X-Gate-Token": "invalid-key"},
            )
        assert resp.status_code == 401
        assert "Invalid" in resp.json()["detail"]

    def test_valid_token_passes_auth(self, client: TestClient) -> None:
        from orchestrator.auth import generate_api_key

        key = generate_api_key("test")
        mock_result = {
            "gate": "lint", "status": "pass",
            "output": "", "exit_code": 0, "duration_ms": 1,
        }
        with (
            patch.dict(os.environ, {"CDMAE_LOCAL_DEV": "0"}, clear=False),
            patch("orchestrator.orchestrator.call_gate", return_value=mock_result),
        ):
            resp = client.post(
                "/commit",
                json=PAYLOAD,
                headers={"X-Gate-Token": key},
            )
        # Should get past auth (may fail at gate calls, but not 401)
        assert resp.status_code != 401

    def test_local_dev_bypass_skips_auth(self, client: TestClient) -> None:
        mock_result = {
            "gate": "lint", "status": "pass",
            "output": "", "exit_code": 0, "duration_ms": 1,
        }
        with (
            patch.dict(os.environ, {"CDMAE_LOCAL_DEV": "1"}, clear=False),
            patch("orchestrator.orchestrator.call_gate", return_value=mock_result),
        ):
            resp = client.post("/commit", json=PAYLOAD)
        assert resp.status_code != 401

    def test_get_endpoints_skip_auth(self, client: TestClient) -> None:
        with patch.dict(os.environ, {"CDMAE_LOCAL_DEV": "0"}, clear=False):
            resp = client.get("/status")
        assert resp.status_code == 200
