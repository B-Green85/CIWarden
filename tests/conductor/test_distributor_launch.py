"""Tests for launch_agent_window pacing — the multi-window clipboard race fix.

A DAG session opens agent windows programmatically. pbcopy writes the single global macOS
pasteboard, so back-to-back windows would each clobber the other's prompt before the
operator could paste. The fix: the Conductor (not the window) owns the clipboard — after
each launch it copies that agent's prompt and pauses ``LAUNCH_PASTE_DELAY`` seconds so the
operator can paste before the next window opens.
"""
from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from conductor import distributor
from conductor.dag import DAGBuilder
from conductor.session import AgentSpec, ConductorSession

if TYPE_CHECKING:
    from pathlib import Path
    from unittest.mock import _Call

    import pytest

_CONSUMER_PROMPT = (
    "# Consumer agent\n\nCONTRACTS_CONSUMED:\n- core-types: Thing (Agent 1)\n"
)
_ROOT_PROMPT = "# Root agent\n\nNo dependencies here.\n"


def _session(tmp_path: Path, agents: list[AgentSpec]) -> ConductorSession:
    return ConductorSession(
        repo_root=tmp_path / "repo",
        repo_name="demo",
        agents=agents,
        staging_base=tmp_path / "base",
        staging_root=tmp_path / "staging",
    )


def _one_agent_session(tmp_path: Path) -> ConductorSession:
    agent = AgentSpec("agent_001", "memory allocator", "build the allocator", "memory_allocator")
    session = _session(tmp_path, [agent])
    distributor.distribute(session)
    return session


def _runs_matching(run_mock: MagicMock, argv0: str) -> list[_Call]:
    """Recorded subprocess.run calls whose argv begins with ``argv0`` (e.g. 'pbcopy')."""
    return [
        c for c in run_mock.call_args_list
        if c.args and isinstance(c.args[0], list) and c.args[0] and c.args[0][0] == argv0
    ]


def test_launch_pauses_for_paste_delay_after_window(tmp_path: Path) -> None:
    session = _one_agent_session(tmp_path)
    with patch("conductor.distributor.subprocess.run"), \
         patch("conductor.distributor.time.sleep") as sleep:
        distributor.launch_agent_window(session, session.agents[0])
    sleep.assert_called_once_with(distributor.LAUNCH_PASTE_DELAY)
    assert distributor.LAUNCH_PASTE_DELAY == 1.5


def test_paste_delay_is_overridable(tmp_path: Path) -> None:
    session = _one_agent_session(tmp_path)
    with patch("conductor.distributor.subprocess.run"), \
         patch("conductor.distributor.time.sleep") as sleep:
        distributor.launch_agent_window(session, session.agents[0], paste_delay=0.25)
    sleep.assert_called_once_with(0.25)


def test_launch_copies_this_agents_prompt_to_clipboard(tmp_path: Path) -> None:
    session = _one_agent_session(tmp_path)
    expected = (distributor.staging_dir(session, session.agents[0]) / distributor.PROMPT_FILE).read_text()
    with patch("conductor.distributor.subprocess.run") as run, \
         patch("conductor.distributor.time.sleep"):
        distributor.launch_agent_window(session, session.agents[0])
    pbcopy = _runs_matching(run, "pbcopy")
    assert len(pbcopy) == 1
    assert pbcopy[0].kwargs["input"] == expected


def test_window_command_does_not_pbcopy(tmp_path: Path) -> None:
    # The Conductor is the sole clipboard writer; the window must NOT pbcopy itself, or a
    # slow-starting window could clobber a later agent's prompt after its window is up.
    session = _one_agent_session(tmp_path)
    with patch("conductor.distributor.subprocess.run") as run, \
         patch("conductor.distributor.time.sleep"):
        distributor.launch_agent_window(session, session.agents[0])
    osascript = _runs_matching(run, "osascript")
    assert len(osascript) == 1
    do_script = " ".join(osascript[0].args[0])
    assert "pbcopy" not in do_script


def test_launch_prints_ready_message_with_delay(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    session = _one_agent_session(tmp_path)
    with patch("conductor.distributor.subprocess.run"), \
         patch("conductor.distributor.time.sleep"):
        distributor.launch_agent_window(session, session.agents[0])
    out = capsys.readouterr().out
    assert "agent_001 ready" in out
    assert "⌘V to paste prompt" in out
    assert "next window opens in 1.5s" in out


def test_window_opens_then_clipboard_then_pause(tmp_path: Path) -> None:
    # Ordering matters: the window must open and the clipboard be set BEFORE the pause —
    # so the operator always has the right prompt to paste during the pause.
    session = _one_agent_session(tmp_path)
    events: list[str] = []

    def record_run(argv: list[str], *_a: object, **_k: object) -> MagicMock:
        events.append("osascript" if argv and argv[0] == "osascript" else "pbcopy")
        return MagicMock(returncode=0, stdout="", stderr="")

    def record_sleep(_seconds: float) -> None:
        events.append("sleep")

    with patch("conductor.distributor.subprocess.run", side_effect=record_run), \
         patch("conductor.distributor.time.sleep", side_effect=record_sleep):
        distributor.launch_agent_window(session, session.agents[0])

    assert events == ["osascript", "pbcopy", "sleep"]


def test_each_launch_in_a_wave_gets_its_own_paste_delay(tmp_path: Path) -> None:
    # Two agents (consumer depends on root) launch one after the other through the DAG
    # release loop; each real launch must contribute exactly one LAUNCH_PASTE_DELAY pause.
    a1 = AgentSpec("agent_001", "root", _ROOT_PROMPT, "agent_1")
    a2 = AgentSpec("agent_002", "consumer", _CONSUMER_PROMPT, "agent_2")
    session = _session(tmp_path, [a1, a2])
    distributor.distribute(session)
    prompt_map = {
        a.agent_id: distributor.staging_dir(session, a) / distributor.PROMPT_FILE
        for a in session.agents
    }
    session.dag = DAGBuilder().build(prompt_map)
    for a in session.agents:
        node = session.dag.get(a.agent_id)
        if node is not None:
            a.depends_on = node.depends_on
        distributor.clear_done(session, a)

    sleeps: list[float] = []

    def launch_then_signal(s: ConductorSession, agent: AgentSpec) -> None:
        distributor.launch_agent_window(s, agent)  # real launch (subprocess + sleep patched)
        (distributor.staging_dir(s, agent) / distributor.DONE_FLAG).write_text("")

    with patch("conductor.distributor.subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")), \
         patch("conductor.distributor.time.sleep", side_effect=sleeps.append):
        ok = distributor.run_dependency_aware(
            session, launch_fn=launch_then_signal, poll_interval=0.01,
        )

    assert ok is True
    # One paste pause per launched agent (poll-loop sleeps use poll_interval, not 1.5).
    assert sleeps.count(distributor.LAUNCH_PASTE_DELAY) == 2
