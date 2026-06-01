"""Tests for scripts.update_commit_log — git + audit-db → commit_log.jsonl."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from typing import TYPE_CHECKING

import pytest

from scripts import update_commit_log as ucl

if TYPE_CHECKING:
    from pathlib import Path

_GATE_MS = {"lint": 10, "typecheck": 20, "security": 30, "memory": 40, "test": 50, "stress": 60, "build": 70}
_TOTAL = sum(_GATE_MS.values())  # 280


def _make_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()

    def g(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    g("init", "-q")
    g("config", "user.email", "t@example.com")
    g("config", "user.name", "Tester")
    (repo / "a.txt").write_text("hello\nworld\n")
    g("add", "a.txt")
    g("-c", "commit.gpgsign=false", "commit", "-q", "-m", "feat: initial")
    sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"]).decode().strip()
    return repo, sha


def _make_db(tmp_path: Path, sha: str, *, empty: bool = False, agent: str = "local-dev") -> Path:
    db = tmp_path / "gate_results.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE gate_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, commit_sha TEXT, agent_id TEXT, "
        "branch TEXT, gate_name TEXT, status TEXT, output TEXT, exit_code INTEGER, duration_ms INTEGER, timestamp REAL)"
    )
    conn.execute(
        "CREATE TABLE merge_tokens (token TEXT PRIMARY KEY, commit_sha TEXT, issued_at REAL, used INTEGER DEFAULT 0)"
    )
    if not empty:
        t = 1000.0
        for name, ms in _GATE_MS.items():
            conn.execute(
                "INSERT INTO gate_runs (commit_sha, agent_id, branch, gate_name, status, "
                "output, exit_code, duration_ms, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sha, agent, "main", name, "pass", "", 0, ms, t),
            )
            t += 1
        conn.execute("INSERT INTO merge_tokens (token, commit_sha, issued_at) VALUES (?, ?, ?)", ("tok123", sha, t))
    conn.commit()
    conn.close()
    return db


@pytest.fixture()
def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path]:
    repo, sha = _make_repo(tmp_path)
    db = _make_db(tmp_path, sha)
    log = tmp_path / ".cdmad" / "commit_log.jsonl"
    monkeypatch.setattr(ucl, "REPO_DIR", repo)
    monkeypatch.setattr(ucl, "DB_PATH", db)
    monkeypatch.setattr(ucl, "LOG_PATH", log)
    return sha, log


class TestAppend:
    def test_correct_structure(self, wired: tuple[str, Path]) -> None:
        sha, log = wired
        assert ucl.append_entry(sha) is True
        entry = json.loads(log.read_text().splitlines()[0])
        assert entry["sha"] == sha
        assert entry["short_sha"] == sha[:7]
        assert entry["token"] == "tok123"
        assert entry["message"] == "feat: initial"
        assert entry["author"] == "local-dev"
        assert entry["files_changed"] == 1
        assert entry["insertions"] == 2
        assert entry["deletions"] == 0
        assert set(entry["gates"]) == set(_GATE_MS)
        assert entry["gates"]["lint"] == {"status": "PASS", "ms": 10}
        assert entry["total_ms"] == _TOTAL
        assert entry["consumed"] is False

    def test_creates_file_if_missing(self, wired: tuple[str, Path]) -> None:
        sha, log = wired
        assert not log.exists()
        ucl.append_entry(sha)
        assert log.exists()

    def test_idempotent(self, wired: tuple[str, Path]) -> None:
        sha, log = wired
        assert ucl.append_entry(sha) is True
        assert ucl.append_entry(sha) is False
        assert len(log.read_text().splitlines()) == 1

    def test_never_overwrites_existing(self, wired: tuple[str, Path]) -> None:
        sha, log = wired
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(json.dumps({"sha": "deadbeefcafe", "message": "older"}) + "\n")
        ucl.append_entry(sha)
        lines = log.read_text().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["sha"] == "deadbeefcafe"
        assert json.loads(lines[1])["sha"] == sha


class TestMissingAuditRow:
    def test_git_fields_only_token_null(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        repo, sha = _make_repo(tmp_path)
        db = _make_db(tmp_path, sha, empty=True)  # tables exist, no rows
        log = tmp_path / ".cdmad" / "commit_log.jsonl"
        monkeypatch.setattr(ucl, "REPO_DIR", repo)
        monkeypatch.setattr(ucl, "DB_PATH", db)
        monkeypatch.setattr(ucl, "LOG_PATH", log)

        ucl.append_entry(sha)
        entry = json.loads(log.read_text().splitlines()[0])
        assert entry["token"] is None
        assert entry["gates"] is None
        assert entry["total_ms"] is None
        # git fields still populated; author falls back to git author
        assert entry["message"] == "feat: initial"
        assert entry["author"] == "Tester"
        assert entry["files_changed"] == 1
