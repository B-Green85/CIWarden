"""Conductor CLI — setup wizard and session orchestration.

Run with ``python3 -m conductor``. The wizard collects agent count (1–12), each
agent's subsystem path / module_key / dependencies / prompt, confirms, then drives:
prime VDB → write metadata → distribute → wait for agents → proof → atomic commit.
"""

from __future__ import annotations

import asyncio

from conductor import distributor
from conductor.commit import atomic_commit
from conductor.prover import initialize_vdb_metadata, proof_session
from conductor.session import AgentSpec, ConductorSession
from conductor.vdb_io import _get_repo_name, prime_vdb_from_codebase

MAX_AGENTS = 12


def _log(message: str) -> None:
    print(message)


async def run_session(session: ConductorSession) -> bool:
    """Execute a fully-specified session end to end. Returns True on clean commit."""
    await prime_vdb_from_codebase(session)
    initialize_vdb_metadata(session)
    distributor.distribute(session)

    _log(f"CONDUCTOR  ● distributed to {len(session.agents)} agents — waiting for .done")
    distributor.wait_for_done(session, session.agents)

    _log("CONDUCTOR  ● proofing in dependency order...")
    if not proof_session(session):
        _log("CONDUCTOR  ✗ proofing did not converge (timed out)")
        return False

    files = atomic_commit(session)
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
    from pathlib import Path

    repo_root = Path.cwd()
    repo_name = _get_repo_name(repo_root)
    _log(f"CONDUCTOR — repo: {repo_name}")

    count = _ask_int(f"How many agents? (1-{MAX_AGENTS})", 1, MAX_AGENTS)
    agents: list[AgentSpec] = []
    for i in range(1, count + 1):
        agent_id = f"agent_{i:03d}"
        subsystem = input(f"  [{agent_id}] subsystem path: ").strip()
        module_key = input(f"  [{agent_id}] module_key: ").strip() or subsystem.strip("/").split("/")[-1]
        deps_raw = input(f"  [{agent_id}] dependencies (comma-sep, blank=none): ")
        deps = [d.strip() for d in deps_raw.split(",") if d.strip()]
        _log(f"  [{agent_id}] prompt (end with a line containing only '.'):")
        prompt = _read_multiline()
        agents.append(AgentSpec(agent_id, subsystem, prompt, module_key, deps))

    _log(f"\nLaunch {count} agents on {repo_name}? [y/N]")
    if input().strip().lower() != "y":
        msg = "Conductor launch aborted by operator"
        raise SystemExit(msg)

    return ConductorSession(repo_root=repo_root, repo_name=repo_name, agents=agents)


def main(argv: list[str] | None = None) -> int:
    session = wizard()
    ok = asyncio.run(run_session(session))
    return 0 if ok else 1
