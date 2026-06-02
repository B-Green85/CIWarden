"""Conductor CLI — setup wizard and session orchestration.

Run with ``python3 -m conductor``. The wizard collects the target repo path, the
agent count (1–12), and each agent's description + prompt, confirms, then drives:
prime VDB → write metadata → distribute → wait for agents → proof → atomic commit.

``python3 -m conductor --resume`` skips the wizard and rebuilds the session from the
existing staging state (a failed attempt's prompts are reused as-is), regenerates the
launch script, and waits for fresh ``.done`` signals.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from conductor import distributor
from conductor.commit import atomic_commit
from conductor.prover import initialize_vdb_metadata, proof_session
from conductor.session import (
    DEFAULT_STAGING_ROOT,
    SESSION_MANIFEST,
    AgentSpec,
    ConductorSession,
    module_key_from_description,
)
from conductor.vdb_io import _get_repo_name, prime_vdb_from_codebase

MAX_AGENTS = 12


def _log(message: str) -> None:
    print(message)


async def run_session(session: ConductorSession, *, resume: bool = False) -> bool:
    """Execute a fully-specified session end to end. Returns True on clean commit.

    With ``resume=True`` the staging dirs and prompts already exist, so distribution
    is skipped (it would overwrite each PROMPT.md and reset agent schemas).

    Before proofing we always clear stale ``.done`` flags and block on a fresh signal
    from every agent — the Conductor must never enter proofing on leftover flags or an
    empty staging dir.
    """
    await prime_vdb_from_codebase(session)
    initialize_vdb_metadata(session)
    if resume:
        _log(f"CONDUCTOR  ● resumed session {session.session_id} — {len(session.agents)} agents in staging")
    else:
        distributor.distribute(session)
        session.save_manifest()
        _log(f"CONDUCTOR  ● distributed to {len(session.agents)} agents")

    script_path = distributor.write_launch_script(session)
    _log("CONDUCTOR  ● agents ready — launch them:")
    _log(f"           bash {script_path}")

    if not session.agents:
        _log("CONDUCTOR  ✗ no agents in this session — nothing to wait for or proof")
        return False

    # Clear any stale .done (from a prior or failed attempt) so the wait blocks for
    # the agents the operator is about to (re)launch — never proof on leftover flags.
    for agent in session.agents:
        distributor.clear_done(session, agent)

    _log("CONDUCTOR  ● waiting for .done from all agents...")
    if not distributor.wait_for_done(session, session.agents):
        _log("CONDUCTOR  ✗ timed out waiting for .done — not entering proofing")
        return False

    _log("CONDUCTOR  ● proofing in dependency order...")
    if not proof_session(session):
        _log("CONDUCTOR  ✗ proofing did not converge (timed out)")
        return False

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
    for i in range(1, count + 1):
        agent_id = f"agent_{i:03d}"
        description = input(f"  [{agent_id}] description (e.g. 'memory allocator'): ").strip()
        module_key = module_key_from_description(description)
        _log(f"  [{agent_id}] prompt (end with a line containing only '.'):")
        prompt = _read_multiline()
        agents.append(AgentSpec(agent_id, description, prompt, module_key))

    _log(f"\nLaunch {count} agents on {repo_name}? [y/N]")
    if input().strip().lower() != "y":
        msg = "Conductor launch aborted by operator"
        raise SystemExit(msg)

    return ConductorSession(repo_root=repo_root, repo_name=repo_name, agents=agents)


# ── Resume ───────────────────────────────────────────────────────


def resume_session(staging_root: Path = DEFAULT_STAGING_ROOT) -> ConductorSession:
    """Rebuild a session from existing staging state for ``--resume``.

    Requires the manifest written at distribute time and a PROMPT.md in every agent
    dir; raises SystemExit with a clear message if either is missing.
    """
    manifest = staging_root / SESSION_MANIFEST
    if not manifest.exists():
        msg = f"--resume: no session manifest at {manifest} — nothing to resume. Run the wizard first."
        raise SystemExit(msg)

    session = ConductorSession.from_manifest(staging_root)
    missing = [
        a.agent_id
        for a in session.agents
        if not (distributor.staging_dir(session, a) / distributor.PROMPT_FILE).exists()
    ]
    if missing:
        msg = f"--resume: missing {distributor.PROMPT_FILE} for {', '.join(missing)} under {staging_root}"
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
