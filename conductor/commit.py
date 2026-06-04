"""Atomic commit — the last authority before code reaches the repo.

Once every agent is clean, the Conductor: (1) commits the contract corpus to the VDB
atomically, (2) copies staged files into the worktree, (3) enqueues ONE commit-queue
entry with the full file list, and (4) deletes the staging directory. The queue worker
performs the single ``git commit`` and drives the gate chain — the Conductor never
commits git itself.

Ordering is critical: the VDB ``commit_session`` MUST precede the queue entry, so the
managed-mode memory gate reads the committed session (N vs N-1), not stale state.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from conductor import distributor
from conductor.vdb_io import store_for
from memory.schema_capture import SchemaCaptureClient

if TYPE_CHECKING:
    from collections.abc import Callable

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


def ensure_worker(session: ConductorSession) -> bool:
    """Start a detached queue worker for the target repo if none is running.

    The worker MUST point at the target repo (``session.repo_root``), not the ciwarden
    repo — it owns the git index there, performs the single commit, and drives the gate
    chain. It is detached (``start_new_session``) so it outlives this Conductor process
    and keeps draining the queue. Returns True if a worker was started, False if one was
    already running. Logs ``CONDUCTOR  ● queue worker started for {repo_name}`` on launch.
    """
    repo_root = session.repo_root.resolve()
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
    _log(f"CONDUCTOR  ● queue worker started for {session.repo_name}")
    return True


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


def staging_root_exists(staging_root: Path) -> bool:
    return staging_root.exists()
