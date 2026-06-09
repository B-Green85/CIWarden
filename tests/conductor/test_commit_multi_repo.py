"""Tests for conductor.commit.atomic_commit_multi_repo — per-repo, topological commits."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock, patch

from conductor import commit, distributor
from conductor.dag import DAGBuilder
from conductor.router import RepoRouter
from conductor.session import AgentSpec, ConductorSession

if TYPE_CHECKING:
    from pathlib import Path


def _prompt(num: int, consumes: int | None) -> str:
    block = ""
    if consumes is not None:
        block = f"\nCONTRACTS_CONSUMED:\n- thing: T (Agent {consumes})\n"
    return f"# agent {num}\n{block}\nCONTRACTS_PRODUCED:\n- stuff\n"


def _agent(num: int, *, repo: str, consumes: int | None) -> AgentSpec:
    return AgentSpec(
        agent_id=f"agent_{num:03d}",
        description=f"agent {num}",
        prompt=_prompt(num, consumes),
        module_key=f"agent_{num}",
        repo=repo,
    )


def _build_session(tmp_path: Path) -> tuple[ConductorSession, RepoRouter, Path, Path]:
    """agent_1 (default, root) → agent_2 (default) → agent_3 (golem).

    Topological order [agent_001, agent_002, agent_003]; repos first appear default then
    golem, so default (root's repo) must commit before golem (which depends on it).
    """
    default_repo = tmp_path / "ciwarden"
    golem_repo = tmp_path / "golem"
    default_repo.mkdir()
    golem_repo.mkdir()

    agents = [
        _agent(1, repo="default", consumes=None),
        _agent(2, repo="default", consumes=1),
        _agent(3, repo="golem", consumes=2),
    ]
    session = ConductorSession(
        repo_root=default_repo,
        repo_name="ciwarden",
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
    for a in agents:
        a.depends_on = session.dag[a.agent_id].depends_on
    session.multi_repo = True

    # Each agent stages one deliverable file.
    for a in agents:
        (distributor.staging_dir(session, a) / f"{a.agent_id}.py").write_text("x = 1\n")

    router = RepoRouter(default_repo_path=default_repo)
    router.register("golem", golem_repo)
    for a in agents:
        router.assign_agent(a.agent_id, a.repo)

    return session, router, default_repo, golem_repo


class TestAtomicCommitMultiRepo:
    def test_one_entry_per_repo_in_topological_order(self, tmp_path: Path) -> None:
        session, router, _default, _golem = _build_session(tmp_path)
        calls: list[dict[str, object]] = []

        def record(agent_id: str, files: list[str], message: str) -> None:
            calls.append({"agent_id": agent_id, "files": files, "message": message})

        with patch.object(commit, "store_for", return_value=MagicMock()), \
             patch.object(commit, "assemble_summaries", return_value=[]), \
             patch.object(commit, "_ensure_worker_for_path", return_value=True), \
             patch.object(router, "enqueue_commit", side_effect=record):
            files: list[str] = commit.atomic_commit_multi_repo(session, router, cleanup=False)

        # Exactly one queue entry per repo.
        assert len(calls) == 2
        # Default repo (the root's repo) is enqueued before golem (which depends on it).
        assert calls[0]["agent_id"] == "agent_001"   # default repo's first agent
        assert calls[1]["agent_id"] == "agent_003"   # golem's first (only) agent
        # Default's single entry covers both of its agents' files.
        assert set(cast("list[str]", calls[0]["files"])) == {"agent_001.py", "agent_002.py"}
        assert calls[1]["files"] == ["agent_003.py"]
        assert set(files) == {"agent_001.py", "agent_002.py", "agent_003.py"}

    def test_files_copied_to_correct_repo(self, tmp_path: Path) -> None:
        session, router, default_repo, golem_repo = _build_session(tmp_path)

        with patch.object(commit, "store_for", return_value=MagicMock()), \
             patch.object(commit, "assemble_summaries", return_value=[]), \
             patch.object(commit, "_ensure_worker_for_path", return_value=True), \
             patch.object(router, "enqueue_commit"):
            commit.atomic_commit_multi_repo(session, router, cleanup=False)

        # Default-repo agents land in the default repo; the golem agent lands in golem.
        assert (default_repo / "agent_001.py").exists()
        assert (default_repo / "agent_002.py").exists()
        assert (golem_repo / "agent_003.py").exists()
        # The golem agent's file must NOT have leaked into the default repo.
        assert not (default_repo / "agent_003.py").exists()

    def test_vdb_committed_once_before_enqueue(self, tmp_path: Path) -> None:
        session, router, _default, _golem = _build_session(tmp_path)
        store = MagicMock()

        order: list[str] = []
        store.commit_session.side_effect = lambda *_a, **_k: order.append("vdb")

        def record(*_a: object, **_k: object) -> None:
            order.append("enqueue")

        with patch.object(commit, "store_for", return_value=store), \
             patch.object(commit, "assemble_summaries", return_value=[]), \
             patch.object(commit, "_ensure_worker_for_path", return_value=True), \
             patch.object(router, "enqueue_commit", side_effect=record):
            commit.atomic_commit_multi_repo(session, router, cleanup=False)

        # VDB corpus commit happens exactly once, before any queue entry.
        assert order == ["vdb", "enqueue", "enqueue"]
        store.advance_session.assert_called_once()

    def test_empty_session_returns_empty_without_vdb(self, tmp_path: Path) -> None:
        session, router, _default, _golem = _build_session(tmp_path)
        # Remove every staged deliverable so nothing is collectable.
        for a in session.agents:
            (distributor.staging_dir(session, a) / f"{a.agent_id}.py").unlink()
        store = MagicMock()

        with patch.object(commit, "store_for", return_value=store), \
             patch.object(commit, "assemble_summaries", return_value=[]), \
             patch.object(commit, "_ensure_worker_for_path"), \
             patch.object(router, "enqueue_commit") as enq:
            files: list[str] = commit.atomic_commit_multi_repo(session, router, cleanup=False)
        assert files == []
        store.commit_session.assert_not_called()
        enq.assert_not_called()
