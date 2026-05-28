"""Tests for queue.commit_queue — multi-agent commit queue."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Load commit_queue.py by path — the `queue/` directory has no __init__.py
# (deliberately, to avoid shadowing the stdlib `queue` module).
_MODULE_PATH = Path(__file__).resolve().parents[2] / "queue" / "commit_queue.py"
_spec = importlib.util.spec_from_file_location("commit_queue", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
commit_queue = importlib.util.module_from_spec(_spec)
sys.modules["commit_queue"] = commit_queue
_spec.loader.exec_module(commit_queue)


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    return repo


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "queue.db"
    monkeypatch.setattr(commit_queue, "QUEUE_DIR", tmp_path)
    monkeypatch.setattr(commit_queue, "DB_PATH", db)
    monkeypatch.setattr(commit_queue, "LOG_PATH", tmp_path / "queue.log")
    return db


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["git"], returncode=returncode, stdout=stdout, stderr=stderr)


class TestEnqueue:
    def test_inserts_pending_row(self, fake_repo: Path, isolated_db: Path) -> None:
        eid = commit_queue.enqueue(str(fake_repo), ["a.py"], "msg", "agent-1")
        assert eid > 0

        conn = sqlite3.connect(isolated_db)
        row = conn.execute(
            "SELECT repo, agent_id, files, message, status FROM commits WHERE id = ?",
            (eid,),
        ).fetchone()
        conn.close()
        assert row[0] == str(fake_repo.resolve())
        assert row[1] == "agent-1"
        assert json.loads(row[2]) == ["a.py"]
        assert row[3] == "msg"
        assert row[4] == "pending"

    def test_rejects_non_git_repo(self, tmp_path: Path, isolated_db: Path) -> None:
        not_a_repo = tmp_path / "nope"
        not_a_repo.mkdir()
        with pytest.raises(RuntimeError, match="Not a git repository"):
            commit_queue.enqueue(str(not_a_repo), ["a.py"], "msg", "agent-1")

    def test_rejects_empty_files(self, fake_repo: Path, isolated_db: Path) -> None:
        with pytest.raises(ValueError, match="at least one file"):
            commit_queue.enqueue(str(fake_repo), [], "msg", "agent-1")

    def test_rejects_blank_message(self, fake_repo: Path, isolated_db: Path) -> None:
        with pytest.raises(ValueError, match="cannot be empty"):
            commit_queue.enqueue(str(fake_repo), ["a.py"], "  ", "agent-1")


class TestClaimNext:
    def test_returns_none_when_empty(self, fake_repo: Path, isolated_db: Path) -> None:
        commit_queue.init_db()
        assert commit_queue._claim_next(str(fake_repo.resolve())) is None

    def test_claims_oldest_pending_and_marks_processing(
        self, fake_repo: Path, isolated_db: Path
    ) -> None:
        first = commit_queue.enqueue(str(fake_repo), ["a.py"], "first", "agent-a")
        commit_queue.enqueue(str(fake_repo), ["b.py"], "second", "agent-b")

        claimed = commit_queue._claim_next(str(fake_repo.resolve()))
        assert claimed is not None
        assert claimed["id"] == first
        assert claimed["files"] == ["a.py"]

        conn = sqlite3.connect(isolated_db)
        status = conn.execute(
            "SELECT status, started_at FROM commits WHERE id = ?", (first,)
        ).fetchone()
        conn.close()
        assert status[0] == "processing"
        assert status[1] is not None


class TestWaitForIndexLock:
    def test_returns_true_when_no_lock(self, fake_repo: Path) -> None:
        assert commit_queue.wait_for_index_lock(str(fake_repo), timeout=0.1, poll=0.01)

    def test_returns_false_on_timeout(self, fake_repo: Path) -> None:
        (fake_repo / ".git" / "index.lock").write_text("")
        assert not commit_queue.wait_for_index_lock(str(fake_repo), timeout=0.05, poll=0.01)


class TestProcessEntry:
    def _entry(self, fake_repo: Path, entry_id: int = 1) -> dict[str, Any]:
        return {
            "id": entry_id,
            "repo": str(fake_repo.resolve()),
            "agent_id": "agent-x",
            "files": ["a.py"],
            "message": "feat: x",
            "enqueued_at": 0.0,
        }

    def test_happy_path_marks_done_and_issues_token(
        self, fake_repo: Path, isolated_db: Path
    ) -> None:
        eid = commit_queue.enqueue(str(fake_repo), ["a.py"], "feat: x", "agent-x")

        run_outputs = [
            _completed(0),                              # git add
            _completed(0),                              # git commit
            _completed(0, stdout="abc1234567890\n"),    # rev-parse HEAD
            _completed(0, stdout="main\n"),             # rev-parse --abbrev-ref HEAD
        ]
        post_response = MagicMock()
        post_response.json.return_value = {"merge_token": "MERGE-TOKEN-123"}
        post_response.raise_for_status.return_value = None

        with patch.object(commit_queue, "_run_git", side_effect=run_outputs), \
             patch.object(commit_queue.httpx, "post", return_value=post_response):
            commit_queue.process_entry(self._entry(fake_repo, eid), MagicMock())

        conn = sqlite3.connect(isolated_db)
        row = conn.execute(
            "SELECT status, commit_sha, merge_token, queue_token FROM commits WHERE id = ?",
            (eid,),
        ).fetchone()
        token_row = conn.execute(
            "SELECT commit_entry_id, commit_sha FROM queue_tokens WHERE token = ?",
            (row[3],),
        ).fetchone()
        conn.close()

        assert row[0] == "done"
        assert row[1] == "abc1234567890"
        assert row[2] == "MERGE-TOKEN-123"
        assert row[3] and len(row[3]) == 24
        assert token_row == (eid, "abc1234567890")

    def test_git_commit_failure_marks_failed(
        self, fake_repo: Path, isolated_db: Path
    ) -> None:
        eid = commit_queue.enqueue(str(fake_repo), ["a.py"], "feat: x", "agent-x")

        run_outputs = [
            _completed(0),                                       # git add
            _completed(1, stderr="nothing to commit, clean"),    # git commit
        ]
        with patch.object(commit_queue, "_run_git", side_effect=run_outputs):
            commit_queue.process_entry(self._entry(fake_repo, eid), MagicMock())

        conn = sqlite3.connect(isolated_db)
        row = conn.execute(
            "SELECT status, error FROM commits WHERE id = ?", (eid,)
        ).fetchone()
        conn.close()
        assert row[0] == "failed"
        assert "nothing to commit" in row[1]

    def test_gate_block_marks_failed_with_blocked_at(
        self, fake_repo: Path, isolated_db: Path
    ) -> None:
        eid = commit_queue.enqueue(str(fake_repo), ["a.py"], "feat: x", "agent-x")
        run_outputs = [
            _completed(0),
            _completed(0),
            _completed(0, stdout="deadbeef\n"),
            _completed(0, stdout="main\n"),
        ]
        post_response = MagicMock()
        post_response.json.return_value = {
            "merge_token": None,
            "blocked_at": "lint",
            "reason": "E501 line too long",
        }
        post_response.raise_for_status.return_value = None

        with patch.object(commit_queue, "_run_git", side_effect=run_outputs), \
             patch.object(commit_queue.httpx, "post", return_value=post_response):
            commit_queue.process_entry(self._entry(fake_repo, eid), MagicMock())

        conn = sqlite3.connect(isolated_db)
        row = conn.execute(
            "SELECT status, error, commit_sha FROM commits WHERE id = ?", (eid,)
        ).fetchone()
        conn.close()
        assert row[0] == "failed"
        assert "lint" in row[1]
        assert row[2] == "deadbeef"


class TestStatus:
    def test_counts_and_recent(self, fake_repo: Path, isolated_db: Path) -> None:
        commit_queue.enqueue(str(fake_repo), ["a.py"], "one", "agent-a")
        commit_queue.enqueue(str(fake_repo), ["b.py"], "two", "agent-b")
        s = commit_queue.status(str(fake_repo))
        assert s["counts"]["pending"] == 2
        assert s["counts"]["done"] == 0
        assert len(s["recent"]) == 2
        # Newest first
        assert s["recent"][0]["message"] == "two"


class TestCLI:
    def test_enqueue_subcommand(
        self, fake_repo: Path, isolated_db: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc = commit_queue.main([
            "enqueue", "--repo", str(fake_repo),
            "--files", "src/a.py src/b.py",
            "--message", "feat: x",
            "--agent-id", "agent-cli",
        ])
        assert rc == 0
        out = capsys.readouterr().out
        assert "enqueued #1" in out

        conn = sqlite3.connect(isolated_db)
        files = conn.execute("SELECT files FROM commits WHERE id = 1").fetchone()[0]
        conn.close()
        assert json.loads(files) == ["src/a.py", "src/b.py"]

    def test_status_subcommand(
        self, fake_repo: Path, isolated_db: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        commit_queue.enqueue(str(fake_repo), ["a.py"], "msg", "agent-a")
        rc = commit_queue.main(["status", "--repo", str(fake_repo)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "pending:    1" in out


class TestSignalHandler:
    def test_install_signal_handlers_sets_shutdown_flag(self) -> None:
        # Use __dict__ to side-step mypy's static attribute check on the
        # dynamically loaded module.
        commit_queue.__dict__["_shutdown_requested"] = False
        commit_queue._install_signal_handlers()
        import signal as _signal
        handler = _signal.getsignal(_signal.SIGTERM)
        assert callable(handler)
        handler(_signal.SIGTERM, None)
        assert commit_queue.__dict__["_shutdown_requested"] is True
        commit_queue.__dict__["_shutdown_requested"] = False
