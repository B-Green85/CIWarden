"""Atomic commit — the last authority before code reaches the repo.

Once every agent is clean, the Conductor: (1) commits the contract corpus to the VDB
atomically, (2) copies staged files into the worktree, (3) enqueues ONE commit-queue
entry with the full file list, and (4) deletes the staging directory. The queue worker
performs the single ``git commit`` and drives the gate chain — the Conductor never
commits git itself.

Ordering is critical: the VDB ``commit_session`` MUST precede the queue entry, so the
managed-mode memory gate reads the committed session (N vs N-1), not stale state.

For multi-repo sessions, :func:`atomic_commit_multi_repo` fans the file copy and queue
entries out across repos in *topological order* — a producer's repo commits before its
consumers' — while still committing the VDB corpus once, before any queue entry, so the
N-vs-N-1 ordering guarantee holds across the whole session.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from conductor import distributor
from conductor.dag import DAGBuilder
from conductor.vdb_io import store_for
from memory.schema_capture import SchemaCaptureClient

if TYPE_CHECKING:
    from collections.abc import Callable

    from conductor.router import RepoRouter
    from conductor.session import ConductorSession
    from memory.models import ContractSummary

_enqueue: Callable[..., int] | None = None


def _log(message: str) -> None:
    print(message)


def _load_enqueue() -> Callable[..., int]:
    """Load queue.commit_queue.enqueue by path.

    The ``queue/`` directory has no ``__init__.py`` and the name collides with the
    stdlib ``queue`` module, so it cannot be imported normally — load it by path,
    mirroring tests/queue/test_commit_queue.py.
    """
    global _enqueue
    if _enqueue is not None:
        return _enqueue
    module_path = Path(__file__).resolve().parent.parent / "queue" / "commit_queue.py"
    spec = importlib.util.spec_from_file_location("commit_queue", module_path)
    if spec is None or spec.loader is None:
        msg = f"cannot load commit_queue from {module_path}"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _enqueue = module.enqueue
    return _enqueue


def _worker_module_path() -> Path:
    """Path to queue/commit_queue.py — the worker entry point (loaded by path, no package)."""
    return Path(__file__).resolve().parent.parent / "queue" / "commit_queue.py"


def _worker_running(repo_root: Path) -> bool:
    """True if a queue worker process is already draining ``repo_root``.

    Matches the worker command line via ``pgrep -f`` against the resolved repo path, so
    a worker started by start_all.sh, the CLI, or a prior Conductor run is all detected
    the same way. Returns False if pgrep is unavailable — better to risk a second worker
    (they serialize on the DB write lock) than to never start one.
    """
    pattern = f"commit_queue.py worker --repo {repo_root}"
    try:
        result = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def _ensure_worker_for_path(repo_root: Path, repo_name: str) -> bool:
    """Start a detached queue worker for ``repo_root`` if none is running.

    The path-based core behind :func:`ensure_worker`. Shared so multi-repo commits can
    start a worker for every repo, not just the session's primary one. The worker is
    detached (``start_new_session``) so it outlives this Conductor process and keeps
    draining the queue. Returns True if a worker was started, False if one already ran.
    """
    repo_root = repo_root.resolve()
    if _worker_running(repo_root):
        return False

    log_dir = repo_root / ".cdmad"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "queue_worker.log"

    with log_path.open("a") as log_file:
        subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            [sys.executable, str(_worker_module_path()), "worker", "--repo", str(repo_root)],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    _log(f"CONDUCTOR  ● queue worker started for {repo_name}")
    return True


def ensure_worker(session: ConductorSession) -> bool:
    """Start a detached queue worker for the target repo if none is running.

    The worker MUST point at the target repo (``session.repo_root``), not the ciwarden
    repo — it owns the git index there, performs the single commit, and drives the gate
    chain. Returns True if a worker was started, False if one was already running.
    """
    return _ensure_worker_for_path(session.repo_root, session.repo_name)


def assemble_summaries(session: ConductorSession) -> list[ContractSummary]:
    """Convert each agent's declared schema into a ContractSummary for the VDB corpus."""
    summaries: list[ContractSummary] = []
    for agent in session.agents:
        schema_path = distributor.staging_dir(session, agent) / distributor.SCHEMA_TEMPLATE
        client = SchemaCaptureClient(schema_path=schema_path, repo_name=session.repo_name)
        summary = client._from_schema(agent.module_key, f"gen_{session.session_id}", 1)  # noqa: SLF001
        if summary is not None:
            summaries.append(summary)
    return summaries


def copy_staged_to_worktree(session: ConductorSession) -> list[str]:
    """Copy every agent's produced files into the worktree. Returns repo-relative paths."""
    copied: list[str] = []
    for agent in session.agents:
        adir = distributor.staging_dir(session, agent)
        for rel in distributor.collect_files(session, agent):
            src = adir / rel
            dst = session.repo_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied.append(rel)
    return sorted(set(copied))


def atomic_commit(session: ConductorSession, *, cleanup: bool = True) -> list[str]:
    """Commit VDB, copy files, enqueue ONE queue entry. Returns the committed file list.

    Returns an empty list — without advancing the VDB, enqueuing, or tearing down
    staging — when the agents produced no staged files (e.g. they wrote directly to
    the repo instead of their staging dirs). This never raises on an empty session;
    the caller decides how to surface it.

    Single-repo path; unchanged. Multi-repo sessions use
    :func:`atomic_commit_multi_repo`.
    """
    enqueue = _load_enqueue()

    # 1. Copy staged files into the worktree. Do this FIRST: if the agents staged
    #    nothing (e.g. they wrote straight to the repo), bail before mutating the VDB
    #    so we neither advance an empty generation nor enqueue an empty commit. Staging
    #    is left intact for inspection / --resume.
    files = copy_staged_to_worktree(session)
    if not files:
        _log(
            "CONDUCTOR  ⚠ no staged files to commit — agents wrote nothing to their "
            "staging dirs (did they write directly to the repo?); skipping commit",
        )
        return []

    # 2. Commit the VDB corpus atomically — MUST precede the queue entry so the managed
    #    memory gate reads the just-committed session (N vs N-1), not stale state.
    store = store_for(session)
    s = store.get_or_create_session()
    store.advance_session(s)
    store.commit_session(assemble_summaries(session))

    # 3. Enqueue ONE entry with the full file list — the worker does the single commit.
    message = f"feat(conductor): atomic commit session {session.session_id} ({len(session.agents)} agents)"
    enqueue(str(session.repo_root), files, message, agent_id="conductor")

    # 3a. Ensure a queue worker is draining the TARGET repo — start one if not. Without
    #     this the entry sits pending forever unless the operator launched a worker by
    #     hand; the worker points at session.repo_root, never the ciwarden repo.
    ensure_worker(session)

    # 4. Tear down staging.
    if cleanup and session.staging_root.exists():
        shutil.rmtree(session.staging_root)

    return files


# ── Multi-repo commit ─────────────────────────────────────────────


def _copy_agents_to_repo(
    session: ConductorSession, agent_ids: list[str], repo_root: Path,
) -> list[str]:
    """Copy the named agents' staged files into ``repo_root``. Returns repo-relative paths.

    The per-repo analogue of :func:`copy_staged_to_worktree`: only the given agents'
    files are copied, and they land under ``repo_root`` (which may be a repo other than
    the session's primary one). Files are de-duplicated and sorted for a stable entry.
    """
    copied: list[str] = []
    for agent_id in agent_ids:
        agent = session.agent_by_id(agent_id)
        if agent is None:
            continue
        adir = distributor.staging_dir(session, agent)
        for rel in distributor.collect_files(session, agent):
            src = adir / rel
            dst = repo_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied.append(rel)
    return sorted(set(copied))


def atomic_commit_multi_repo(
    session: ConductorSession, router: RepoRouter, *, cleanup: bool = True,
) -> list[str]:
    """Commit all agents' outputs across all repos, in topological order.

    Ordering guarantees:

    * dependencies commit before dependents — repos are visited in the topological
      order of their *first* agent, so a producer's repo is enqueued before any repo
      that consumes from it;
    * within a wave (same dependency depth) ordering is deterministic — alphabetical by
      agent_id, inherited from ``DAGBuilder.topological_order``;
    * each repo receives exactly ONE queue entry covering all its agents' files.

    The VDB corpus is committed once, before any queue entry (preserving the N-vs-N-1
    ordering the managed memory gate relies on). Returns the full committed file list
    across every repo, or an empty list if no agent staged anything.
    """
    builder = DAGBuilder()
    topo_order = builder.topological_order(session.dag)

    # Group agents by repo, preserving topological order within and across repos. A repo
    # first appears at its earliest-in-topo agent, so root-only repos lead.
    repo_order: list[str] = []
    repo_agents: dict[str, list[str]] = {}
    for agent_id in topo_order:
        agent = session.agent_by_id(agent_id)
        if agent is None:
            continue
        repo_name = agent.repo
        if repo_name not in repo_agents:
            repo_agents[repo_name] = []
            repo_order.append(repo_name)
        repo_agents[repo_name].append(agent_id)

    # 1. Copy each repo's staged files into that repo's worktree FIRST, so an empty
    #    session bails before the VDB is advanced (mirrors atomic_commit's ordering).
    repo_files: dict[str, list[str]] = {}
    for repo_name in repo_order:
        repo_ctx = router.repos.get(repo_name)
        repo_root = repo_ctx.path if repo_ctx is not None else session.repo_root
        repo_files[repo_name] = _copy_agents_to_repo(
            session, repo_agents[repo_name], repo_root,
        )

    if not any(repo_files.values()):
        _log(
            "CONDUCTOR  ⚠ no staged files to commit across any repo — agents wrote "
            "nothing to their staging dirs; skipping commit",
        )
        return []

    # 2. Commit the VDB corpus ONCE, before any queue entry. The corpus is the session's
    #    (keyed by session.repo_name); multi-repo file routing does not split it.
    store = store_for(session)
    s = store.get_or_create_session()
    store.advance_session(s)
    store.commit_session(assemble_summaries(session))

    # 3. ONE queue entry per repo, enqueued in topological repo order. Routing is by the
    #    repo's first agent — every agent in the list shares that repo — so the entry
    #    lands on the correct worker.
    committed: list[str] = []
    for repo_name in repo_order:
        files = repo_files[repo_name]
        if not files:
            continue
        # Report the whole session's agent count, not just the agents routed to this repo.
        # A commit is part of a session, and every agent contributed to it — including any
        # that landed in another repo or wrote directly to the repo instead of staging.
        # Per-repo counting is what made a multi-repo Sentinel v3 commit read "7 agents"
        # when the session had 8.
        message = (
            f"feat(conductor): atomic commit session {session.session_id} "
            f"repo={repo_name} ({len(session.agents)} agents)"
        )
        router.enqueue_commit(
            agent_id=repo_agents[repo_name][0],
            files=files,
            message=message,
        )
        committed.extend(files)

        # 3a. Ensure a worker is draining this repo. The gate chain + merge tokens run
        #     per repo inside the worker — no change needed here beyond starting it.
        repo_ctx = router.repos.get(repo_name)
        repo_root = repo_ctx.path if repo_ctx is not None else session.repo_root
        _ensure_worker_for_path(repo_root, repo_name)

    # 4. Tear down staging once every repo's entry is enqueued.
    if cleanup and session.staging_root.exists():
        shutil.rmtree(session.staging_root)

    return sorted(set(committed))


def staging_root_exists(staging_root: Path) -> bool:
    return staging_root.exists()
