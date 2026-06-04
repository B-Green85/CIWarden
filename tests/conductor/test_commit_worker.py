"""Tests for conductor.commit worker auto-start (ensure_worker / _worker_running)."""
from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from conductor import commit
from conductor.session import AgentSpec, ConductorSession

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _session(tmp_path: Path) -> ConductorSession:
    repo = tmp_path / "target_repo"
    repo.mkdir()
    agent = AgentSpec("agent_001", "memory allocator", "p", "memory_allocator")
    return ConductorSession(
        repo_root=repo,
        repo_name="target_repo",
        agents=[agent],
        staging_root=tmp_path / "staging",
    )


def _completed(returncode: int, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["pgrep"], returncode=returncode, stdout=stdout, stderr="")


class TestWorkerRunning:
    def test_true_when_pgrep_matches(self, tmp_path: Path) -> None:
        with patch("conductor.commit.subprocess.run", return_value=_completed(0, "4321\n")):
            assert commit._worker_running(tmp_path) is True

    def test_false_when_pgrep_no_match(self, tmp_path: Path) -> None:
        with patch("conductor.commit.subprocess.run", return_value=_completed(1, "")):
            assert commit._worker_running(tmp_path) is False

    def test_false_when_pgrep_missing(self, tmp_path: Path) -> None:
        with patch("conductor.commit.subprocess.run", side_effect=OSError("no pgrep")):
            assert commit._worker_running(tmp_path) is False


class TestEnsureWorker:
    def test_starts_worker_when_none_running(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        session = _session(tmp_path)
        with patch.object(commit, "_worker_running", return_value=False), \
             patch("conductor.commit.subprocess.Popen", return_value=MagicMock()) as popen:
            started = commit.ensure_worker(session)

        assert started is True
        popen.assert_called_once()
        argv = popen.call_args.args[0]
        # Worker must target the resolved TARGET repo, not the ciwarden repo.
        assert argv[2] == "worker"
        assert argv[3] == "--repo"
        assert argv[4] == str(session.repo_root.resolve())
        assert argv[1].endswith("commit_queue.py")
        # Detached so it outlives the Conductor.
        assert popen.call_args.kwargs.get("start_new_session") is True
        out = capsys.readouterr().out
        assert "queue worker started for target_repo" in out

    def test_skips_when_worker_already_running(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        session = _session(tmp_path)
        with patch.object(commit, "_worker_running", return_value=True), \
             patch("conductor.commit.subprocess.Popen") as popen:
            started = commit.ensure_worker(session)

        assert started is False
        popen.assert_not_called()
        assert "queue worker started" not in capsys.readouterr().out
