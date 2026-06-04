"""Tests for conductor.distributor — staging-only directive and soft-signal detection."""
from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

from conductor import distributor
from conductor.session import AgentSpec, ConductorSession

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _session(tmp_path: Path) -> ConductorSession:
    agent = AgentSpec(
        agent_id="agent_001",
        description="memory allocator",
        prompt="build the allocator",
        module_key="memory_allocator",
    )
    return ConductorSession(
        repo_root=tmp_path / "repo",
        repo_name="demo",
        agents=[agent],
        staging_root=tmp_path / "staging",
    )


def _completed(returncode: int, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["git"], returncode=returncode, stdout=stdout, stderr="")


class TestPromptInjection:
    def test_prompt_includes_staging_only_directive_and_path(self, tmp_path: Path) -> None:
        session = _session(tmp_path)
        distributor.distribute(session)
        agent = session.agents[0]
        prompt = (distributor.staging_dir(session, agent) / distributor.PROMPT_FILE).read_text()

        assert "WRITE ONLY to your staging directory" in prompt
        # The explicit staging path must appear in the injection.
        assert str(distributor.staging_dir(session, agent)) in prompt
        assert "NEVER" in prompt or "Do NOT" in prompt
        assert "touch" in prompt and ".done" in prompt


class TestStagingIsEmpty:
    def test_true_when_no_deliverables(self, tmp_path: Path) -> None:
        session = _session(tmp_path)
        distributor.distribute(session)  # writes only PROMPT.md + .cdmad schema (both excluded)
        assert distributor.staging_is_empty(session, session.agents[0]) is True

    def test_false_when_deliverable_present(self, tmp_path: Path) -> None:
        session = _session(tmp_path)
        distributor.distribute(session)
        adir = distributor.staging_dir(session, session.agents[0])
        (adir / "alloc.py").write_text("x = 1\n")
        assert distributor.staging_is_empty(session, session.agents[0]) is False


class TestRepoUntrackedForAgent:
    def test_matches_untracked_by_module_key_tokens(self, tmp_path: Path) -> None:
        session = _session(tmp_path)
        porcelain = "?? src/memory/allocator.py\n?? README.md\n?? src/network/socket.py\n"
        with patch("conductor.distributor.subprocess.run", return_value=_completed(0, porcelain)):
            matches = distributor.repo_untracked_for_agent(session, session.agents[0])
        assert matches == ["src/memory/allocator.py"]

    def test_ignores_staged_and_modified_lines(self, tmp_path: Path) -> None:
        session = _session(tmp_path)
        # ' M' (modified) and 'A ' (added) must be ignored — only '??' untracked counts.
        porcelain = " M src/memory_allocator.py\nA  memory/allocator.py\n"
        with patch("conductor.distributor.subprocess.run", return_value=_completed(0, porcelain)):
            matches = distributor.repo_untracked_for_agent(session, session.agents[0])
        assert matches == []

    def test_empty_on_git_error(self, tmp_path: Path) -> None:
        session = _session(tmp_path)
        with patch("conductor.distributor.subprocess.run", return_value=_completed(128, "")):
            assert distributor.repo_untracked_for_agent(session, session.agents[0]) == []

    def test_empty_when_git_missing(self, tmp_path: Path) -> None:
        session = _session(tmp_path)
        with patch("conductor.distributor.subprocess.run", side_effect=OSError("no git")):
            assert distributor.repo_untracked_for_agent(session, session.agents[0]) == []


class TestWaitForDone:
    def test_returns_true_when_done_flag_present(self, tmp_path: Path) -> None:
        session = _session(tmp_path)
        distributor.distribute(session)
        adir = distributor.staging_dir(session, session.agents[0])
        (adir / "alloc.py").write_text("x = 1\n")
        (adir / distributor.DONE_FLAG).touch()
        assert distributor.wait_for_done(session, session.agents, poll_interval=0.01) is True

    def test_soft_signal_stops_waiting_and_warns(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        session = _session(tmp_path)
        distributor.distribute(session)  # staging stays empty (no deliverables, no .done)
        porcelain = "?? src/memory/allocator.py\n"
        with patch("conductor.distributor.subprocess.run", return_value=_completed(0, porcelain)):
            # No timeout: without the soft signal this would block forever.
            ok = distributor.wait_for_done(session, session.agents, poll_interval=0.01)
        assert ok is True
        out = capsys.readouterr().out
        assert "soft signal" in out
        assert "agent_001" in out

    def test_warns_only_once(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Two agents, one with a real .done and one stray; loop runs a few iterations
        # for the done agent while the stray must warn exactly once.
        a1 = AgentSpec("agent_001", "memory allocator", "p", "memory_allocator")
        a2 = AgentSpec("agent_002", "network socket", "p", "network_socket")
        session = ConductorSession(
            repo_root=tmp_path / "repo",
            repo_name="demo",
            agents=[a1, a2],
            staging_root=tmp_path / "staging",
        )
        distributor.distribute(session)
        porcelain = "?? src/memory/allocator.py\n"

        calls = {"n": 0}
        real_done = distributor.is_done

        def fake_is_done(s: ConductorSession, agent: AgentSpec) -> bool:
            # a2 becomes done only after a couple of polls, forcing extra iterations.
            if agent.agent_id == "agent_002":
                calls["n"] += 1
                return calls["n"] > 2
            return real_done(s, agent)

        with patch("conductor.distributor.subprocess.run", return_value=_completed(0, porcelain)), \
             patch.object(distributor, "is_done", side_effect=fake_is_done):
            ok = distributor.wait_for_done(session, session.agents, poll_interval=0.01)

        assert ok is True
        assert capsys.readouterr().out.count("soft signal") == 1

    def test_times_out_when_no_signal(self, tmp_path: Path) -> None:
        session = _session(tmp_path)
        distributor.distribute(session)
        # No deliverables, no stray files → genuine wait → must time out, not hang.
        with patch("conductor.distributor.subprocess.run", return_value=_completed(0, "")):
            ok = distributor.wait_for_done(
                session, session.agents, poll_interval=0.01, timeout=0.05
            )
        assert ok is False
