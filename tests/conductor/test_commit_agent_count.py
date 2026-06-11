"""Tests that the atomic-commit message reports the full session agent count.

Regression for the Sentinel v3 bug where a multi-repo commit message read "7 agents" while
the session had 8: the multi-repo path counted only the agents routed to that repo, not the
whole session. The count must be len(session.agents) — every agent in the session, including
any that wrote directly to the repo instead of staging.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

from conductor import commit, distributor
from conductor.dag import DAGBuilder
from conductor.router import RepoRouter
from conductor.session import AgentSpec, ConductorSession

if TYPE_CHECKING:
    from pathlib import Path

_COUNT_RE = re.compile(r"\((\d+) agents\)")


def _agent_count_in(message: str) -> int:
    match = _COUNT_RE.search(message)
    assert match is not None, f"no agent count in commit message: {message!r}"
    return int(match.group(1))


# ── Single-repo ───────────────────────────────────────────────────


def _single_repo_session(tmp_path: Path, *, num_agents: int, num_staging: int) -> ConductorSession:
    """A single-repo session of ``num_agents``, of which only the first ``num_staging``
    stage a deliverable — the rest simulate agents that wrote directly to the repo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    agents = [
        AgentSpec(f"agent_{n:03d}", f"agent {n}", "build it", f"agent_{n}")
        for n in range(1, num_agents + 1)
    ]
    session = ConductorSession(
        repo_root=repo,
        repo_name="demo",
        agents=agents,
        staging_base=tmp_path / "base",
        staging_root=tmp_path / "staging",
    )
    distributor.distribute(session)
    for agent in agents[:num_staging]:
        (distributor.staging_dir(session, agent) / f"{agent.agent_id}.py").write_text("x = 1\n")
    return session


def test_single_repo_message_counts_all_session_agents(tmp_path: Path) -> None:
    # 3 agents, but only 2 staged files (one wrote directly to the repo). The message must
    # still report 3 — the whole session — not 2.
    session = _single_repo_session(tmp_path, num_agents=3, num_staging=2)
    enqueue = MagicMock()
    with patch.object(commit, "_load_enqueue", return_value=enqueue), \
         patch.object(commit, "store_for", return_value=MagicMock()), \
         patch.object(commit, "assemble_summaries", return_value=[]), \
         patch.object(commit, "ensure_worker", return_value=True):
        files = commit.atomic_commit(session, cleanup=False)

    assert files  # the two staged files were committed
    message = enqueue.call_args.args[2]  # enqueue(repo, files, message, agent_id=...)
    assert _agent_count_in(message) == len(session.agents) == 3


# ── Multi-repo ────────────────────────────────────────────────────


def _multi_repo_session(tmp_path: Path) -> tuple[ConductorSession, RepoRouter]:
    """agent_1 (default) → agent_2 (default) → agent_3 (golem): 2 agents on default, 1 on golem.

    Old per-repo counting reported "2 agents" / "1 agents"; the fix reports the session total
    (3) in both messages.
    """
    default_repo = tmp_path / "default"
    golem_repo = tmp_path / "golem"
    default_repo.mkdir()
    golem_repo.mkdir()

    def prompt(num: int, consumes: int | None) -> str:
        block = f"\nCONTRACTS_CONSUMED:\n- thing: T (Agent {consumes})\n" if consumes else ""
        return f"# agent {num}\n{block}\nCONTRACTS_PRODUCED:\n- stuff\n"

    agents = [
        AgentSpec("agent_001", "a1", prompt(1, None), "agent_1", repo="default"),
        AgentSpec("agent_002", "a2", prompt(2, 1), "agent_2", repo="default"),
        AgentSpec("agent_003", "a3", prompt(3, 2), "agent_3", repo="golem"),
    ]
    session = ConductorSession(
        repo_root=default_repo,
        repo_name="demo",
        agents=agents,
        staging_base=tmp_path / "base",
        staging_root=tmp_path / "staging",
    )
    distributor.distribute(session)
    prompt_map = {
        a.agent_id: distributor.staging_dir(session, a) / distributor.PROMPT_FILE
        for a in agents
    }
    session.dag = DAGBuilder().build(prompt_map)
    for agent in agents:
        agent.depends_on = session.dag[agent.agent_id].depends_on
    session.multi_repo = True
    for agent in agents:
        (distributor.staging_dir(session, agent) / f"{agent.agent_id}.py").write_text("x = 1\n")

    router = RepoRouter(default_repo_path=default_repo)
    router.register("golem", golem_repo)
    for agent in agents:
        router.assign_agent(agent.agent_id, agent.repo)
    return session, router


def test_multi_repo_messages_count_all_session_agents(tmp_path: Path) -> None:
    session, router = _multi_repo_session(tmp_path)
    assert len(session.agents) == 3

    messages: list[str] = []

    def record(agent_id: str, files: list[str], message: str) -> None:
        messages.append(message)

    with patch.object(commit, "store_for", return_value=MagicMock()), \
         patch.object(commit, "assemble_summaries", return_value=[]), \
         patch.object(commit, "_ensure_worker_for_path", return_value=True), \
         patch.object(router, "enqueue_commit", side_effect=record):
        commit.atomic_commit_multi_repo(session, router, cleanup=False)

    # One entry per repo (default + golem); BOTH report the full session count (3), not the
    # per-repo subset (2 for default, 1 for golem) that produced the "7 of 8" bug.
    assert len(messages) == 2
    assert [_agent_count_in(m) for m in messages] == [3, 3]
    # And the per-repo subset count must not appear in either message.
    assert not any("(2 agents)" in m or "(1 agents)" in m for m in messages)
