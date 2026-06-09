"""Repo routing for multi-repo Conductor sessions.

When a session spans more than one git repo, each agent's staged output, ``.done``
watching, and commit-queue entry must reach the correct repo's worker. The
:class:`RepoRouter` holds one :class:`RepoContext` per repo and answers three
questions for the rest of the Conductor:

* which repo does this agent write to (``get_repo_for_agent``),
* enqueue this agent's commit to the right repo's queue worker (``enqueue_commit``),
* is a queue worker actually draining each repo (``verify_queue_workers_running``).

The router has *zero* knowledge of the gate chain. It knows only about repos, their
filesystem paths, queue workers, and which agent_ids belong where. The default repo —
the one the Conductor was invoked from — is always registered under the key
``"default"``, so an agent that never set a ``repo`` (the common, single-repo case)
routes to ``session.repo_root`` exactly as today.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# The repo key every agent falls back to: the repo the Conductor was invoked from.
DEFAULT_REPO = "default"

# Worker entry point relative to this file: conductor/router.py → queue/commit_queue.py.
# Derived from __file__ rather than hardcoded so the router works from any checkout.
_COMMIT_QUEUE = Path(__file__).resolve().parent.parent / "queue" / "commit_queue.py"


@dataclass
class RepoContext:
    """One repo's routing state within a session."""

    name: str
    path: Path                  # absolute path to the repo root
    queue_worker_running: bool = False
    agents: list[str] = field(default_factory=list)  # agent_ids assigned to this repo


class RepoRouter:
    """Routes agents to the correct repo's queue worker.

    One :class:`RepoContext` per repo involved in the session. The default repo is
    registered at construction time, so a session where no agent overrides ``repo``
    has exactly one repo — the invoking one — and behaves identically to today.
    """

    def __init__(self, default_repo_path: Path):
        self.repos: dict[str, RepoContext] = {}
        self.default_repo_path = Path(default_repo_path)
        # Always register the default repo so get_repo_for_agent always resolves.
        self._register(DEFAULT_REPO, self.default_repo_path)

    def register(self, repo_name: str, repo_path: Path) -> None:
        """Register a repo by name. Idempotent — re-registering is a no-op."""
        if repo_name not in self.repos:
            self._register(repo_name, Path(repo_path))

    def assign_agent(self, agent_id: str, repo_name: str) -> None:
        """Assign an agent to a repo. Called during session setup.

        The repo must already be registered; assigning to an unknown repo is a setup
        error (the wizard collects the path and registers before assigning), so we
        raise rather than silently routing to the default.
        """
        if repo_name not in self.repos:
            msg = (
                f"Repo '{repo_name}' not registered. "
                f"Register it before assigning agents."
            )
            raise ValueError(msg)
        if agent_id not in self.repos[repo_name].agents:
            self.repos[repo_name].agents.append(agent_id)

    def get_repo_for_agent(self, agent_id: str) -> RepoContext | None:
        """Return the RepoContext for ``agent_id``, or the default repo if unassigned."""
        for repo in self.repos.values():
            if agent_id in repo.agents:
                return repo
        return self.repos.get(DEFAULT_REPO)

    def enqueue_commit(self, agent_id: str, files: list[str], message: str) -> None:
        """Enqueue a commit to the correct repo's queue worker.

        Routes via the agent's assigned repo and shells out to ``commit_queue.py
        enqueue`` for that repo. ``--files`` is one space-separated, shlex-parsed
        string (the queue rejects comma-separated values), so we join with spaces.
        """
        repo = self.get_repo_for_agent(agent_id)
        if repo is None:
            msg = f"No repo found for agent {agent_id}"
            raise ValueError(msg)

        cmd = self._build_enqueue_cmd(repo.path, agent_id, files, message)
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell
            cmd, capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            msg = (
                f"Commit queue enqueue failed for agent {agent_id} "
                f"(repo {repo.name}): {result.stderr.strip() or result.stdout.strip()}"
            )
            raise RuntimeError(msg)

    def verify_queue_workers_running(self) -> list[str]:
        """Return the names of registered repos with no running queue worker.

        Advisory only: the caller warns the operator. An empty list means every repo
        has a draining worker. Updates each RepoContext.queue_worker_running as a side
        effect so callers can inspect per-repo state after the check.
        """
        missing: list[str] = []
        for name, repo in self.repos.items():
            running = self._is_worker_running(repo.path)
            repo.queue_worker_running = running
            if not running:
                missing.append(name)
        return missing

    # ── internals ────────────────────────────────────────────────────────────

    def _register(self, name: str, path: Path) -> None:
        self.repos[name] = RepoContext(name=name, path=Path(path))

    @staticmethod
    def _build_enqueue_cmd(
        repo_path: Path, agent_id: str, files: list[str], message: str,
    ) -> list[str]:
        """Build the ``commit_queue.py enqueue`` argv for one repo.

        Factored out so the command can be asserted on in tests without spawning a
        subprocess. ``--files`` is a single space-joined token (shlex-parsed by the
        queue); ``--agent-id`` records which agent the entry routed through.
        """
        import sys

        return [
            sys.executable,
            str(_COMMIT_QUEUE),
            "enqueue",
            "--repo", str(repo_path),
            "--files", " ".join(files),
            "--message", message,
            "--agent-id", agent_id,
        ]

    @staticmethod
    def _is_worker_running(repo_path: Path) -> bool:
        """True if a queue worker process is draining ``repo_path``.

        Mirrors ``conductor.commit._worker_running``: matches the worker command line
        via ``pgrep -f`` against the resolved repo path, so a worker started by
        start_all.sh, the CLI, or a prior Conductor run is detected the same way.
        Returns False if pgrep is unavailable — the caller only warns, never blocks.
        """
        pattern = f"commit_queue.py worker --repo {Path(repo_path).resolve()}"
        try:
            result = subprocess.run(  # noqa: S603 — fixed argv, no shell
                ["pgrep", "-f", pattern],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return False
        return result.returncode == 0 and bool(result.stdout.strip())
