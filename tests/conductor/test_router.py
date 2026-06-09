"""Tests for conductor.router — repo registration, agent assignment, enqueue routing."""
from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from conductor.router import DEFAULT_REPO, RepoContext, RepoRouter

if TYPE_CHECKING:
    from pathlib import Path


class TestRegistration:
    def test_default_repo_registered_at_construction(self, tmp_path: Path) -> None:
        router = RepoRouter(default_repo_path=tmp_path / "repo")
        assert DEFAULT_REPO in router.repos
        assert router.repos[DEFAULT_REPO].path == tmp_path / "repo"

    def test_register_is_idempotent(self, tmp_path: Path) -> None:
        router = RepoRouter(default_repo_path=tmp_path / "repo")
        router.register("golem", tmp_path / "golem")
        router.register("golem", tmp_path / "elsewhere")  # ignored — already registered
        assert router.repos["golem"].path == tmp_path / "golem"

    def test_register_accepts_str_path(self, tmp_path: Path) -> None:
        router = RepoRouter(default_repo_path=tmp_path / "repo")
        router.register("golem", str(tmp_path / "golem"))  # type: ignore[arg-type]
        assert isinstance(router.repos["golem"], RepoContext)


class TestAssignment:
    def test_assign_unregistered_repo_raises(self, tmp_path: Path) -> None:
        router = RepoRouter(default_repo_path=tmp_path / "repo")
        with pytest.raises(ValueError, match="not registered"):
            router.assign_agent("agent_001", "golem")

    def test_assign_is_deduplicated(self, tmp_path: Path) -> None:
        router = RepoRouter(default_repo_path=tmp_path / "repo")
        router.assign_agent("agent_001", DEFAULT_REPO)
        router.assign_agent("agent_001", DEFAULT_REPO)
        assert router.repos[DEFAULT_REPO].agents == ["agent_001"]

    def test_get_repo_for_assigned_agent(self, tmp_path: Path) -> None:
        router = RepoRouter(default_repo_path=tmp_path / "repo")
        router.register("golem", tmp_path / "golem")
        router.assign_agent("agent_003", "golem")
        ctx = router.get_repo_for_agent("agent_003")
        assert ctx is not None
        assert ctx.name == "golem"

    def test_get_repo_for_unassigned_agent_falls_back_to_default(self, tmp_path: Path) -> None:
        router = RepoRouter(default_repo_path=tmp_path / "repo")
        ctx = router.get_repo_for_agent("agent_999")
        assert ctx is not None
        assert ctx.name == DEFAULT_REPO


class TestEnqueueRouting:
    def test_enqueue_cmd_targets_assigned_repo(self, tmp_path: Path) -> None:
        router = RepoRouter(default_repo_path=tmp_path / "repo")
        router.register("golem", tmp_path / "golem")
        router.assign_agent("agent_003", "golem")

        cmd = router._build_enqueue_cmd(  # noqa: SLF001
            tmp_path / "golem", "agent_003", ["a.py", "b.py"], "msg",
        )
        assert cmd[0] == sys.executable
        assert "enqueue" in cmd
        # --files is one space-joined token (shlex-parsed, comma-rejecting on the far side).
        files_idx = cmd.index("--files")
        assert cmd[files_idx + 1] == "a.py b.py"
        repo_idx = cmd.index("--repo")
        assert cmd[repo_idx + 1] == str(tmp_path / "golem")
        agent_idx = cmd.index("--agent-id")
        assert cmd[agent_idx + 1] == "agent_003"

    def test_enqueue_routes_to_correct_repo_path(self, tmp_path: Path) -> None:
        router = RepoRouter(default_repo_path=tmp_path / "repo")
        router.register("golem", tmp_path / "golem")
        router.assign_agent("agent_003", "golem")

        captured: dict[str, list[str]] = {}

        def fake_run(
            cmd: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch("conductor.router.subprocess.run", side_effect=fake_run):
            router.enqueue_commit("agent_003", ["x.py"], "commit x")

        cmd: list[str] = captured["cmd"]
        assert str(tmp_path / "golem") in cmd  # routed to golem, not the default repo

    def test_enqueue_failure_raises_runtime_error(self, tmp_path: Path) -> None:
        router = RepoRouter(default_repo_path=tmp_path / "repo")
        router.assign_agent("agent_001", DEFAULT_REPO)

        def fake_run(
            cmd: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 1, "", "boom")

        with patch("conductor.router.subprocess.run", side_effect=fake_run), \
             pytest.raises(RuntimeError, match="enqueue failed"):
            router.enqueue_commit("agent_001", ["x.py"], "commit x")


class TestWorkerVerification:
    def test_missing_when_no_worker_running(self, tmp_path: Path) -> None:
        router = RepoRouter(default_repo_path=tmp_path / "repo")
        router.register("golem", tmp_path / "golem")
        with patch.object(RepoRouter, "_is_worker_running", return_value=False):
            missing = router.verify_queue_workers_running()
        assert set(missing) == {DEFAULT_REPO, "golem"}

    def test_none_missing_when_all_running(self, tmp_path: Path) -> None:
        router = RepoRouter(default_repo_path=tmp_path / "repo")
        router.register("golem", tmp_path / "golem")
        with patch.object(RepoRouter, "_is_worker_running", return_value=True):
            missing = router.verify_queue_workers_running()
        assert missing == []
        assert router.repos["golem"].queue_worker_running is True
