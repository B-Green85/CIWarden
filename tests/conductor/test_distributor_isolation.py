"""Tests for conductor.distributor repo isolation — read-only target repos during a session.

Covers the governance invariant that the target repo(s) are read-only while agents run, so
every byte of agent output reaches the repo only through the Conductor's atomic commit:

* chmod is applied before the first agent window opens (DAG path),
* a rogue write into the repo is blocked while isolation is active,
* the exact original permissions are restored once the agent phase ends,
* the cleanup handlers restore on SIGTERM and on an unhandled exception.

The signal / unhandled-exception cases run in a child process: the parent isolates a temp
repo in the child, then kills or crashes it and asserts the repo came back writable.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from textwrap import dedent
from typing import TYPE_CHECKING

import pytest

from conductor import distributor
from conductor.dag import DAGBuilder
from conductor.session import AgentSpec, ConductorSession

if TYPE_CHECKING:
    from collections.abc import Iterator

# tests/conductor/test_distributor_isolation.py → project root is two levels up.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_ROOT_PROMPT = "# Root agent\n\nNo dependencies here.\n"
_CONSUMER_PROMPT = dedent("""\
    # Consumer agent

    CONTRACTS_CONSUMED:
    - core-types: Thing (Agent 1)

    CONTRACTS_PRODUCED:
    - the consumer
    """)


@pytest.fixture(autouse=True)
def _always_restore() -> Iterator[None]:
    """Backstop: restore any lingering isolation after each test.

    The isolator is a module singleton, and a read-only tmp dir would break pytest's
    tmp_path teardown — so we always restore, even if a test isolated without a path that
    restores on its own.
    """
    yield
    distributor.restore_session_repos()


def _agent(num: int, prompt: str) -> AgentSpec:
    aid = f"agent_{num:03d}"
    return AgentSpec(agent_id=aid, description=f"agent {num}", prompt=prompt, module_key=f"agent_{num}")


def _session(tmp_path: Path, agents: list[AgentSpec], repo: Path) -> ConductorSession:
    return ConductorSession(
        repo_root=repo,
        repo_name="demo",
        agents=agents,
        staging_base=tmp_path / "base",
        staging_root=tmp_path / "staging",
    )


def _repo_with_file(tmp_path: Path) -> tuple[Path, Path]:
    """A real repo dir containing one tracked-style file. Returns (repo, file)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    target = repo / "existing.py"
    target.write_text("x = 1\n")
    return repo, target


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _build_dag(session: ConductorSession) -> None:
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


def test_chmod_before_first_agent_launches(tmp_path: Path) -> None:
    repo, target = _repo_with_file(tmp_path)
    session = _session(tmp_path, [_agent(1, _ROOT_PROMPT), _agent(2, _CONSUMER_PROMPT)], repo)
    _build_dag(session)
    assert distributor.has_dependencies(session) is True  # ensure the DAG launch path
    for a in session.agents:
        distributor.clear_done(session, a)

    writable_at_launch: dict[str, bool] = {}

    def stub_launch(s: ConductorSession, agent: AgentSpec) -> None:
        # Capture whether the repo file is writable at the moment this window opens.
        writable_at_launch[agent.agent_id] = os.access(target, os.W_OK)
        (distributor.staging_dir(s, agent) / distributor.DONE_FLAG).write_text("")

    ok = distributor.run_dependency_aware(session, launch_fn=stub_launch, poll_interval=0.01)

    assert ok is True
    assert set(writable_at_launch) == {"agent_001", "agent_002"}
    # Every agent saw a read-only repo — chmod happened before the first launch.
    assert all(writable is False for writable in writable_at_launch.values())


def test_rogue_write_blocked_during_isolation(tmp_path: Path) -> None:
    repo, _ = _repo_with_file(tmp_path)
    session = _session(tmp_path, [_agent(1, _ROOT_PROMPT)], repo)
    distributor.distribute(session)

    # The non-DAG launch path isolates the repo when the launch script is written.
    distributor.write_launch_script(session)
    assert os.access(repo, os.W_OK) is False

    # An agent writing a new file straight into the read-only repo must be denied.
    with pytest.raises(PermissionError):
        (repo / "src" / "rogue.py").write_text("evil = 1\n")
    assert not (repo / "src" / "rogue.py").exists()


def test_restore_after_agent_phase(tmp_path: Path) -> None:
    repo, target = _repo_with_file(tmp_path)
    orig = _mode(target)
    session = _session(tmp_path, [_agent(1, _ROOT_PROMPT)], repo)
    distributor.distribute(session)
    adir = distributor.staging_dir(session, session.agents[0])
    (adir / "out.py").write_text("y = 2\n")
    (adir / distributor.DONE_FLAG).touch()

    distributor.write_launch_script(session)  # isolates
    assert os.access(repo, os.W_OK) is False

    ok = distributor.wait_for_done(session, session.agents, poll_interval=0.01)  # restores

    assert ok is True
    assert os.access(repo, os.W_OK) is True
    assert _mode(target) == orig  # exact original permissions restored
    # And the worktree is writable again, so the atomic commit's copy can land.
    dst = repo / "src" / "out.py"
    dst.write_text("y = 2\n")
    assert dst.exists()


def _isolating_child_source(repo: Path, base: Path, staging: Path, *, tail: str, ready: Path | None = None) -> str:
    """Source for a child that isolates ``repo`` then runs ``tail`` (a crash or a wait).

    Built as flat, zero-indent lines so interpolated paths can never disturb the source's
    indentation (a multi-line value would defeat a dedent applied after interpolation).
    """
    lines = [
        "from pathlib import Path",
        "from conductor import distributor",
        "from conductor.session import AgentSpec, ConductorSession",
        (
            f"session = ConductorSession(repo_root=Path({str(repo)!r}), repo_name='demo', "
            f"agents=[AgentSpec('agent_001', 'a', 'p', 'k')], "
            f"staging_base=Path({str(base)!r}), staging_root=Path({str(staging)!r}))"
        ),
        "distributor.isolate_session_repos(session)",
    ]
    if ready is not None:
        lines.append(f"Path({str(ready)!r}).write_text('go')")
    lines.append(tail)
    return "\n".join(lines) + "\n"


def _run_child(source: str, *, stderr: int | None = None) -> subprocess.Popen[bytes]:
    env = {**os.environ, "PYTHONPATH": str(_PROJECT_ROOT)}
    return subprocess.Popen(  # noqa: S603 — fixed argv, no shell
        [sys.executable, "-c", source],
        cwd=str(_PROJECT_ROOT),
        env=env,
        stderr=stderr,
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal semantics")
def test_restore_on_sigterm(tmp_path: Path) -> None:
    import signal
    import time

    repo, target = _repo_with_file(tmp_path)
    orig = _mode(target)
    ready = tmp_path / "ready"
    source = _isolating_child_source(
        repo, tmp_path / "base", tmp_path / "staging",
        ready=ready, tail="import time; time.sleep(30)",
    )

    proc = _run_child(source)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "child never finished isolating"
        assert os.access(target, os.W_OK) is False  # child made it read-only

        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()

    assert rc != 0  # died from the signal, did not exit cleanly
    assert os.access(target, os.W_OK) is True  # SIGTERM handler restored it
    assert _mode(target) == orig


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process semantics")
def test_restore_on_unhandled_exception(tmp_path: Path) -> None:
    repo, target = _repo_with_file(tmp_path)
    orig = _mode(target)
    source = _isolating_child_source(
        repo, tmp_path / "base", tmp_path / "staging",
        tail="raise RuntimeError('conductor crashed mid-session')",
    )

    proc = _run_child(source, stderr=subprocess.PIPE)
    _, stderr = proc.communicate(timeout=10)

    assert proc.returncode != 0
    assert b"RuntimeError" in stderr  # the crash really propagated unhandled
    assert os.access(target, os.W_OK) is True  # atexit restored it anyway
    assert _mode(target) == orig
