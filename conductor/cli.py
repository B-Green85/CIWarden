"""Conductor CLI — setup wizard and session orchestration.

Run with ``python3 -m conductor``. The wizard collects the target repo path, the
agent count (1–12), and each agent's description + prompt + optional target repo,
confirms, then drives: prime VDB → write metadata → distribute → build the dependency
graph → wait for agents (in dependency order if any are declared) → proof → atomic
commit (per-repo, in topological order, for multi-repo sessions).

``python3 -m conductor --resume`` skips the wizard and rebuilds the session from the
existing staging state (a failed attempt's prompts, DAG, repo assignments, and per-agent
states are all reused as-is) and waits for fresh ``.done`` signals.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from conductor import distributor
from conductor.commit import atomic_commit, atomic_commit_multi_repo
from conductor.dag import DAGBuilder
from conductor.prover import initialize_vdb_metadata, proof_session
from conductor.router import RepoRouter
from conductor.session import (
    DEFAULT_STAGING_ROOT,
    SESSION_MANIFEST,
    AgentSpec,
    ConductorSession,
    module_key_from_description,
)
from conductor.vdb_io import _get_repo_name, prime_vdb_from_codebase

MAX_AGENTS = 12

# Sidecar next to the session manifest mapping a repo name → its absolute path. The
# session manifest itself (owned by session.py) carries each agent's repo *name* but not
# the filesystem path, so the router's path lookups live here, written by the wizard and
# read on --resume. Single-repo sessions never write it.
REPO_PATHS_FILE = "repo_paths.json"


def _log(message: str) -> None:
    print(message)


# ── Repo-path sidecar ─────────────────────────────────────────────


def save_repo_paths(staging_base: Path, mapping: dict[str, str]) -> None:
    """Persist the repo-name → path map for the router (no-op when single-repo)."""
    if not mapping:
        return
    staging_base.mkdir(parents=True, exist_ok=True)
    (staging_base / REPO_PATHS_FILE).write_text(json.dumps(mapping, indent=2))


def load_repo_paths(staging_base: Path) -> dict[str, str]:
    """Read the repo-name → path map written by the wizard ({} if absent/invalid)."""
    path = staging_base / REPO_PATHS_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


# ── DAG + router wiring ───────────────────────────────────────────


def _print_dependency_summary(session: ConductorSession) -> None:
    _log("\nDependency graph:")
    for agent_id in sorted(session.dag):
        node = session.dag[agent_id]
        if node.depends_on:
            _log(f"  {agent_id} waits for: {', '.join(node.depends_on)}")
        else:
            _log(f"  {agent_id} — no dependencies (launches immediately)")
    _log("")


def build_dag(session: ConductorSession) -> None:
    """Build the dependency graph from the distributed prompts and wire it into the session.

    Called after ``distribute`` has written each agent's PROMPT.md (DAGBuilder parses the
    CONTRACTS_CONSUMED block out of those files). Populates ``session.dag``, mirrors each
    node's ``depends_on`` onto its AgentSpec, sets ``multi_repo``, and prints a summary so
    the operator can see the launch ordering before any window opens. A session whose
    prompts declare no dependencies yields a graph with no edges — identical to today.
    """
    builder = DAGBuilder()
    prompt_map = {
        agent.agent_id: distributor.staging_dir(session, agent) / distributor.PROMPT_FILE
        for agent in session.agents
    }
    session.dag = builder.build(prompt_map)

    for agent in session.agents:
        node = session.dag.get(agent.agent_id)
        if node is not None:
            agent.depends_on = node.depends_on

    session.multi_repo = any(a.repo != "default" for a in session.agents)
    _print_dependency_summary(session)


def build_router(session: ConductorSession, repo_paths: dict[str, str]) -> RepoRouter:
    """Construct the RepoRouter, registering each non-default repo and assigning agents.

    The default repo (``session.repo_root``) is registered by the router's constructor.
    For an agent whose ``repo`` has no configured path we warn and route it to the default
    rather than aborting — the commit still lands, just in the invoking repo.
    """
    router = RepoRouter(default_repo_path=session.repo_root)
    for agent in session.agents:
        if agent.repo != "default":
            rpath = repo_paths.get(agent.repo)
            if rpath is None:
                _log(
                    f"CONDUCTOR  ⚠ no path configured for repo '{agent.repo}' "
                    f"(agent {agent.agent_id}) — routing to the default repo",
                )
                router.assign_agent(agent.agent_id, "default")
                continue
            router.register(agent.repo, Path(rpath))
        router.assign_agent(agent.agent_id, agent.repo)
    return router


def _verify_workers(router: RepoRouter, *, interactive: bool) -> None:
    """Warn (and, when interactive, pause) if any repo lacks a draining queue worker."""
    missing = router.verify_queue_workers_running()
    if not missing:
        return
    _log(f"\nWARNING: Queue workers not detected for: {missing}")
    _log("Start workers with: python3 ~/Projects/ciwarden/queue/commit_queue.py worker --repo <path>")
    _log("Ensure workers are running before agents complete.")
    if interactive and sys.stdin.isatty():
        input("Press Enter to continue anyway, or Ctrl+C to abort...")


# ── Session orchestration ─────────────────────────────────────────


async def run_session(session: ConductorSession, *, resume: bool = False) -> bool:
    """Execute a fully-specified session end to end. Returns True on clean commit.

    With ``resume=True`` the staging dirs, prompts, DAG, and per-agent states already
    exist (rebuilt from the manifest), so distribution and DAG construction are skipped.

    Launch path is selected by the DAG: if any agent declares a dependency the Conductor
    opens windows itself, in dependency order (no launch_agents.sh); otherwise the classic
    launch_agents.sh + wait-for-all path runs, byte-for-byte as before. Commit path is
    selected by ``multi_repo``: per-repo topological commit, or the single atomic commit.
    """
    await prime_vdb_from_codebase(session)
    initialize_vdb_metadata(session)
    if resume:
        _log(f"CONDUCTOR  ● resumed session {session.session_id} — {len(session.agents)} agents in staging")
    else:
        distributor.distribute(session)
        build_dag(session)
        session.save_manifest()
        _log(f"CONDUCTOR  ● distributed to {len(session.agents)} agents")

    if not session.agents:
        _log("CONDUCTOR  ✗ no agents in this session — nothing to wait for or proof")
        return False

    # Build the router for multi-repo sessions and check each repo has a worker.
    router: RepoRouter | None = None
    if session.multi_repo:
        repo_paths = load_repo_paths(session.staging_base)
        router = build_router(session, repo_paths)
        _verify_workers(router, interactive=not resume)

    dag_mode = distributor.has_dependencies(session)

    # On a fresh distribute, clear any stale .done (from a prior/failed attempt) so we
    # block for the agents about to launch. On --resume keep existing .done: those are the
    # very signals resume recovers; clearing them would block forever on signals never
    # rewritten. (Both launch paths share this rule.)
    if not resume:
        for agent in session.agents:
            distributor.clear_done(session, agent)

    if dag_mode:
        _log("CONDUCTOR  ● dependency-ordered launch — opening each agent's window as its dependencies clear")
        if not distributor.run_dependency_aware(session):
            _log("CONDUCTOR  ✗ dependency-aware launch did not complete (timeout or stall)")
            return False
    else:
        script_path = distributor.write_launch_script(session)
        _log("CONDUCTOR  ● agents ready — launch them:")
        _log(f"           bash {script_path}")
        _log("CONDUCTOR  ● waiting for .done from all agents...")
        if not distributor.wait_for_done(session, session.agents):
            _log("CONDUCTOR  ✗ timed out waiting for .done — not entering proofing")
            return False

    _log("CONDUCTOR  ● proofing in dependency order...")
    if not proof_session(session):
        _log("CONDUCTOR  ✗ proofing did not converge (timed out)")
        return False

    if session.multi_repo and router is not None:
        files = atomic_commit_multi_repo(session, router)
    else:
        files = atomic_commit(session)
    if not files:
        _log("CONDUCTOR  ✗ nothing committed — agents staged no files (see warning above)")
        return False
    _log(f"CONDUCTOR  ✓ atomic commit enqueued — {len(files)} files, session {session.session_id}")
    return True


# ── Interactive wizard ───────────────────────────────────────────


def _ask_int(prompt: str, low: int, high: int) -> int:
    while True:
        raw = input(f"{prompt} ").strip()
        try:
            value = int(raw)
        except ValueError:
            _log(f"  enter a number between {low} and {high}")
            continue
        if low <= value <= high:
            return value
        _log(f"  must be between {low} and {high}")


def _read_multiline(end_marker: str = ".") -> str:
    lines: list[str] = []
    while True:
        line = input()
        if line.strip() == end_marker:
            break
        lines.append(line)
    return "\n".join(lines)


def wizard() -> ConductorSession:
    repo_root = Path(input("Target repo path: ").strip() or ".").expanduser().resolve()
    repo_name = _get_repo_name(repo_root)
    _log(f"CONDUCTOR — repo: {repo_name} ({repo_root})")

    count = _ask_int(f"How many agents? (1-{MAX_AGENTS})", 1, MAX_AGENTS)
    agents: list[AgentSpec] = []
    repo_paths: dict[str, str] = {}
    for i in range(1, count + 1):
        agent_id = f"agent_{i:03d}"
        description = input(f"  [{agent_id}] description (e.g. 'memory allocator'): ").strip()
        module_key = module_key_from_description(description)
        _log(f"  [{agent_id}] prompt (end with a line containing only '.'):")
        prompt = _read_multiline()
        # New: per-agent target repo. Pressing Enter keeps "default" (the invoking repo)
        # and asks nothing further — identical to the pre-upgrade wizard. Naming a repo
        # prompts once for its path, reused for every later agent on the same repo.
        repo = input(f"  [{agent_id}] repo [default]: ").strip() or "default"
        if repo != "default" and repo not in repo_paths:
            rpath = input(f"  Path to {repo} repo: ").strip()
            repo_paths[repo] = str(Path(rpath).expanduser().resolve())
        agents.append(AgentSpec(agent_id, description, prompt, module_key, repo=repo))

    _log(f"\nLaunch {count} agents on {repo_name}? [y/N]")
    if input().strip().lower() != "y":
        msg = "Conductor launch aborted by operator"
        raise SystemExit(msg)

    session = ConductorSession(repo_root=repo_root, repo_name=repo_name, agents=agents)
    session.multi_repo = any(a.repo != "default" for a in agents)
    save_repo_paths(session.staging_base, repo_paths)
    return session


# ── Resume ───────────────────────────────────────────────────────


def resume_session(staging_base: Path = DEFAULT_STAGING_ROOT) -> ConductorSession:
    """Rebuild a session from existing staging state for ``--resume``.

    Reads the manifest at the staging base (written at distribute time) and requires a
    PROMPT.md in every agent's namespaced staging dir; raises SystemExit with a clear
    message if either is missing. The DAG, repo assignments, and per-agent states are
    restored from the manifest by ``ConductorSession.from_manifest``.
    """
    manifest = staging_base / SESSION_MANIFEST
    if not manifest.exists():
        msg = f"--resume: no session manifest at {manifest} — nothing to resume. Run the wizard first."
        raise SystemExit(msg)

    session = ConductorSession.from_manifest(staging_base)
    missing = [
        a.agent_id
        for a in session.agents
        if not (distributor.staging_dir(session, a) / distributor.PROMPT_FILE).exists()
    ]
    if missing:
        msg = f"--resume: missing {distributor.PROMPT_FILE} for {', '.join(missing)} under {session.staging_root}"
        raise SystemExit(msg)

    _log(f"CONDUCTOR — resuming session {session.session_id}: {session.repo_name} ({session.repo_root})")
    return session


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="conductor", description="CDMAD multi-agent Conductor")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="skip the wizard and relaunch agents from existing staging state",
    )
    args = parser.parse_args(argv)

    session = resume_session() if args.resume else wizard()
    ok = asyncio.run(run_session(session, resume=args.resume))
    return 0 if ok else 1
