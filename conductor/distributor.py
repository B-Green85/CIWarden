"""Distribution + agent-signalling protocol for the Conductor.

Owns the staging directory layout, prompt injection (including the COMMIT PROTOCOL
additions and the ``module_key`` stamp), the ``launch_agents.sh`` generator that opens
one Terminal window per agent, and the ``.done`` / ``.conflict_report.txt`` filesystem
handshake the agents use to signal completion and receive conflict reports.

Two launch models coexist:

* **Non-DAG sessions** (no agent declares a dependency) keep today's behaviour exactly:
  ``write_launch_script`` emits ``launch_agents.sh``, the operator runs it to open every
  agent's window at once, and ``wait_for_done`` blocks until all have signalled.
* **DAG sessions** (at least one ``depends_on`` edge) are managed directly by the
  Conductor: ``run_dependency_aware`` opens each agent's window only once every
  dependency has signalled ``.done``, persisting each agent's state to the manifest as it
  moves through waiting → ready → running → done. No ``launch_agents.sh`` is generated for
  these — launch ordering is the whole point, and a script that opened everything at once
  would defeat it.

The selector is :func:`has_dependencies`; ``cli.run_session`` picks the path.
"""

from __future__ import annotations

import json
import subprocess
import time
from typing import TYPE_CHECKING, Any

from conductor.dag import DAGBuilder

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from conductor.session import AgentSpec, ConductorSession

DONE_FLAG = ".done"
CONFLICT_REPORT_FILE = ".conflict_report.txt"
SCHEMA_TEMPLATE = ".cdmad/session_schema.json"
LAUNCH_SCRIPT = "launch_agents.sh"
PROMPT_FILE = "PROMPT.md"


def _log(message: str) -> None:
    print(message)

_COMMIT_PROTOCOL = """\

--- COMMIT PROTOCOL (Conductor-managed) ---
WRITE ONLY to your staging directory:
  {staging_dir}
Every file you produce MUST live under that exact path. Do NOT write, edit, or
create files anywhere else in the target repository, and do NOT run git yourself.
The Conductor copies your staging dir into the repo atomically after proofing —
files written directly to the repo are NOT collected, will NOT be committed, and
break the gate chain. Staging is the only path your output reaches the repo.

When your files are complete, signal the Conductor:
  touch {staging_dir}/.done

Before resubmitting after a conflict, read carefully:
  {staging_dir}/.conflict_report.txt
Address EVERY point in the report before rewriting.
Do not resubmit until all points are resolved.
"""


def staging_dir(session: ConductorSession, agent: AgentSpec) -> Path:
    return agent.staging_dir(session.staging_root)


def distribute(session: ConductorSession) -> None:
    """Create each agent's staging directory and write its prompt + schema template."""
    for agent in session.agents:
        adir = staging_dir(session, agent)
        adir.mkdir(parents=True, exist_ok=True)
        _write_prompt(adir, agent)
        _write_schema_template(adir, session, agent)


def _write_prompt(adir: Path, agent: AgentSpec) -> None:
    body = agent.prompt + _COMMIT_PROTOCOL.format(staging_dir=adir)
    (adir / PROMPT_FILE).write_text(body)


def _write_schema_template(adir: Path, session: ConductorSession, agent: AgentSpec) -> None:
    """Stamp the agent's session_schema.json with module_key + identifiers.

    The agent fills in modules/exposes/consumes; the Conductor owns module_key so
    the gate's VDB lookups resolve regardless of directory naming.
    """
    empty_module: dict[str, list[Any]] = {
        "contracts": [],
        "exposes": [],
        "consumes": [],
        "assumptions": [],
        "dependencies": [],
    }
    template = {
        "module_key": agent.module_key,
        "repo": session.repo_name,
        "session_id": session.session_id,
        "modules": {agent.module_key: empty_module},
    }
    schema_path = adir / SCHEMA_TEMPLATE
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path.write_text(json.dumps(template, indent=2))


# ── Launch script (non-DAG sessions only) ────────────────────────

# Header is raw so the backslash-escapes survive verbatim into the file. The heredoc
# is intentionally unquoted: ``$1`` expands to the staging dir at launch time, while
# the ``\"`` around the echo message are escaped so they reach osascript untouched.
# claude runs interactively (no -p) — that path needs no API key and works with Max.
# pbcopy targets the single global macOS pasteboard, so the windows MUST be opened one
# at a time: write_launch_script emits a ``read`` pause after each launch so the operator
# pastes (⌘V) into that window before the next pbcopy overwrites the clipboard. Opening
# every window at once lets the last pbcopy win and every agent gets the last agent's
# prompt (the Phase 4 "every agent got Agent 6's prompt" failure).
# The osascript do-script line, fragmented across source lines to stay under the line
# limit; implicit concatenation yields one physical line in the generated script. The
# literal quotes wrap the AppleScript string; the inner \" are escaped for osascript.
_DO_SCRIPT_LINE = (
    r'''    do script "cd '$1' && cat PROMPT.md | pbcopy && '''
    r'''echo \"Prompt copied to clipboard — paste with ⌘V\" && '''
    r'''claude --dangerously-skip-permissions"'''
)

_LAUNCH_HEADER = "\n".join([
    "#!/usr/bin/env bash",
    "# Auto-generated by the Conductor — launches one Terminal window per agent.",
    "# Each window cd's into the agent's staging dir, copies PROMPT.md to the clipboard,",
    "# then launches claude interactively for the operator to paste into.",
    "# Windows open ONE AT A TIME: the script pauses for you to paste (⌘V) into each",
    "# window before opening the next, because pbcopy uses the single global clipboard",
    "# and launching them all at once leaves the last agent's prompt on every window.",
    "# Regenerated on every distribute; do not edit by hand.",
    "# NOTE: generated ONLY for non-DAG sessions. When agents declare dependencies the",
    "# Conductor opens windows itself, in dependency order — see run_dependency_aware.",
    "set -euo pipefail",
    "",
    "launch() {",
    "    osascript <<OSA",
    'tell application "Terminal"',
    "    activate",
    _DO_SCRIPT_LINE,
    "end tell",
    "OSA",
    "}",
    "",
    "",
])


def write_launch_script(session: ConductorSession) -> Path:
    """Write launch_agents.sh under the staging root and return its path.

    The script opens one Terminal window per agent (via osascript), each cd'ing into
    that agent's staging dir, copying the already-written PROMPT.md to the clipboard,
    and launching claude interactively for the operator to paste the prompt into.

    Windows are launched serially with a ``read`` pause between them: pbcopy writes to
    the single global macOS pasteboard, so the operator must paste into each window
    before the next window's pbcopy overwrites the clipboard. Opening every window at
    once would leave the last agent's prompt on the clipboard for all of them — the
    Phase 4 failure where every agent received Agent 6's prompt.

    Generated only for non-DAG sessions; DAG sessions are launched by
    ``run_dependency_aware`` instead.
    """
    agents = session.agents
    total = len(agents)
    lines: list[str] = []
    for i, agent in enumerate(agents, start=1):
        lines.append(f"launch '{staging_dir(session, agent)}'")
        tail = (
            "then press Enter to launch the next agent... "
            if i < total
            else "then press Enter to finish. "
        )
        prompt = f"Agent {i}/{total} launched — paste the prompt into its window with ⌘V, {tail}"
        lines.append(f'read -r -p "{prompt}"')
    body = _LAUNCH_HEADER + "\n".join(lines) + "\n"
    script_path = session.staging_root / LAUNCH_SCRIPT
    session.staging_root.mkdir(parents=True, exist_ok=True)
    script_path.write_text(body)
    script_path.chmod(0o755)
    return script_path


# ── Single-agent launch (DAG sessions) ───────────────────────────


def launch_agent_window(session: ConductorSession, agent: AgentSpec) -> None:
    """Open one Terminal window for a single agent — the DAG-session launch primitive.

    Mirrors what ``launch_agents.sh`` does per agent (cd into the staging dir, copy that
    agent's PROMPT.md to the clipboard, launch claude interactively for the operator to
    paste), but for exactly one agent, on demand, when its dependencies have cleared.
    Because dependency ordering staggers launches in time, the global-clipboard race that
    forced the serial ``read`` pauses in the batch script is largely avoided here — each
    window copies its own PROMPT.md as it opens. Within a single ready-wave the operator
    should still paste promptly into each window as it appears.

    Best-effort: osascript failures (e.g. a headless host with no Terminal) are logged,
    not raised, so one un-openable window never wedges the release loop.
    """
    adir = staging_dir(session, agent)
    inner = (
        f"cd '{adir}' && cat {PROMPT_FILE} | pbcopy && "
        "echo 'Prompt copied to clipboard - paste with Cmd-V' && "
        "claude --dangerously-skip-permissions"
    )
    try:
        subprocess.run(  # noqa: S603 — fixed argv, no shell
            [
                "osascript",
                "-e", 'tell application "Terminal"',
                "-e", "activate",
                "-e", f'do script "{inner}"',
                "-e", "end tell",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        _log(f"CONDUCTOR  ⚠ could not open window for {agent.agent_id}: {exc}")


# ── State machine persistence ─────────────────────────────────────

# Valid per-agent states; persisted to the session manifest so --resume knows where each
# agent left off. Transitions: waiting → ready → running → done, with running → failed
# (PeerChecker conflict), failed → running (retry), and waiting → blocked (a dependency
# failed with no retry).
VALID_STATES = frozenset({"waiting", "ready", "running", "done", "failed", "blocked"})


def update_state(session: ConductorSession, agent_id: str, state: str) -> None:
    """Write a new state for one agent and persist the manifest.

    The manifest on disk is what makes ``--resume`` aware of where each agent is: a
    crash between any two polls leaves every agent's last-written state recoverable.
    Unknown ``agent_id`` is a no-op; an invalid ``state`` raises so a typo can't silently
    persist a state the resume logic won't recognise.
    """
    if state not in VALID_STATES:
        msg = f"invalid agent state {state!r} — expected one of {sorted(VALID_STATES)}"
        raise ValueError(msg)
    agent = session.agent_by_id(agent_id)
    if agent is None:
        return
    agent.state = state
    session.save_manifest()


# ── Agent signalling ─────────────────────────────────────────────


def is_done(session: ConductorSession, agent: AgentSpec) -> bool:
    return (staging_dir(session, agent) / DONE_FLAG).exists()


def clear_done(session: ConductorSession, agent: AgentSpec) -> None:
    flag = staging_dir(session, agent) / DONE_FLAG
    if flag.exists():
        flag.unlink()


def write_conflict_report(session: ConductorSession, agent: AgentSpec, report: str) -> None:
    """Write the conflict report and clear .done so the agent re-signals when fixed."""
    adir = staging_dir(session, agent)
    (adir / CONFLICT_REPORT_FILE).write_text(report)
    clear_done(session, agent)


def read_agent_schema(session: ConductorSession, agent: AgentSpec) -> dict[str, Any]:
    """Read the agent's produced session_schema.json (empty dict if absent/invalid)."""
    schema_path = staging_dir(session, agent) / SCHEMA_TEMPLATE
    if not schema_path.exists():
        return {}
    try:
        data = json.loads(schema_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def collect_files(session: ConductorSession, agent: AgentSpec) -> list[str]:
    """Repo-relative file paths the agent produced under its staging root."""
    adir = staging_dir(session, agent)
    out: list[str] = []
    for path in sorted(adir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(adir)
        if rel.name in {DONE_FLAG, CONFLICT_REPORT_FILE, PROMPT_FILE}:
            continue
        if rel.parts and rel.parts[0] == ".cdmad":
            continue
        out.append(str(rel))
    return out


def staging_is_empty(session: ConductorSession, agent: AgentSpec) -> bool:
    """True if the agent has produced no deliverable files in its staging dir."""
    return not collect_files(session, agent)


def repo_untracked_for_agent(session: ConductorSession, agent: AgentSpec) -> list[str]:
    """Untracked repo files whose path matches the agent's module_key.

    Soft-signal heuristic: when an agent ignores the staging-only protocol and writes
    deliverables straight into the target repo, its staging dir stays empty but new
    untracked files matching its module_key appear. Matching is by module_key tokens
    (split on ``_``, dropping tokens shorter than 3 chars) — every token must appear
    in the lowercased path, so ``memory_allocator`` matches ``src/memory/allocator.py``.
    Returns [] on any git error; this is advisory only, never authoritative.
    """
    tokens = [t for t in agent.module_key.lower().split("_") if len(t) >= 3]
    if not tokens:
        return []
    try:
        result = subprocess.run(
            ["git", "-C", str(session.repo_root), "status",
             "--porcelain", "--untracked-files=all"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    if result.returncode != 0:
        return []
    matches: list[str] = []
    for line in result.stdout.splitlines():
        if not line.startswith("??"):
            continue
        path = line[3:].strip()
        lowered = path.lower()
        if all(tok in lowered for tok in tokens):
            matches.append(path)
    return matches


def wait_for_done(
    session: ConductorSession,
    agents: list[AgentSpec],
    poll_interval: float = 1.0,
    timeout: float | None = None,
) -> bool:
    """Block until all given agents have signalled. Returns True if all signalled.

    An agent signals normally by writing ``.done``. As a fallback, an agent whose
    staging dir is empty but whose module_key matches new untracked files in the repo
    is treated as a *soft signal*: it wrote to the repo instead of staging (violating
    the protocol), so we warn once and stop waiting on it rather than blocking forever.
    ``atomic_commit`` will then surface the empty staging and refuse the commit.

    This is the non-DAG path — every agent is already running. DAG sessions use
    :func:`run_dependency_aware`, which releases agents as their dependencies clear.
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    pending = list(agents)
    warned: set[str] = set()
    while pending:
        still_pending: list[AgentSpec] = []
        for a in pending:
            if is_done(session, a):
                continue
            if staging_is_empty(session, a):
                stray = repo_untracked_for_agent(session, a)
                if stray:
                    if a.agent_id not in warned:
                        warned.add(a.agent_id)
                        _log(
                            f"CONDUCTOR  ⚠ {a.agent_id} ({a.module_key}) staged nothing "
                            f"but wrote {len(stray)} matching file(s) directly to the repo "
                            f"(e.g. {stray[0]}) — treating as soft signal, not waiting. "
                            "Agents MUST write to staging only.",
                        )
                    continue
            still_pending.append(a)
        pending = still_pending
        if not pending:
            return True
        if deadline is not None and time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval)
    return True


# ── Dependency-aware release loop (DAG sessions) ──────────────────


def has_dependencies(session: ConductorSession) -> bool:
    """True if any agent declares a dependency — the gate that selects the DAG path.

    A session whose DAG has no edges (no prompt declared ``CONTRACTS_CONSUMED``) is
    behaviourally identical to today: every agent is ready from the first poll. Such a
    session takes the non-DAG path (``write_launch_script`` + ``wait_for_done``) so the
    backward-compatibility guarantee holds exactly. Only an actual dependency edge flips
    the Conductor into managed, dependency-ordered launching.
    """
    return any(node.depends_on for node in session.dag.values())


def _agent_done(session: ConductorSession, agent: AgentSpec, warned: set[str]) -> bool:
    """True if ``agent`` has signalled — normally via ``.done``, or via a soft signal.

    Shares the soft-signal heuristic with ``wait_for_done`` so the two launch paths agree
    on what "finished" means: a ``.done`` file, or (protocol violation) an empty staging
    dir alongside matching untracked repo files. Warns once per soft-signalled agent.
    """
    if is_done(session, agent):
        return True
    if staging_is_empty(session, agent):
        stray = repo_untracked_for_agent(session, agent)
        if stray:
            if agent.agent_id not in warned:
                warned.add(agent.agent_id)
                _log(
                    f"CONDUCTOR  ⚠ {agent.agent_id} ({agent.module_key}) staged nothing "
                    f"but wrote {len(stray)} matching file(s) directly to the repo "
                    f"(e.g. {stray[0]}) — treating as soft signal, not waiting. "
                    "Agents MUST write to staging only.",
                )
            return True
    return False


def run_dependency_aware(
    session: ConductorSession,
    *,
    launch_fn: Callable[[ConductorSession, AgentSpec], None] | None = None,
    poll_interval: float = 1.0,
    timeout: float | None = None,
) -> bool:
    """Release agents as their dependencies clear; block until all are done.

    The managed launch loop for DAG sessions. Each pass:

    1. marks newly-signalled agents ``done`` (``.done`` or soft signal),
    2. releases every agent whose dependencies are now all done — moving it
       waiting → ready → running and opening its window via ``launch_fn``,
    3. marks ``blocked`` any not-yet-launched agent with a failed dependency.

    Every transition is persisted via :func:`update_state`, so a crash mid-session is
    recoverable: on ``--resume`` agents already ``done`` are seeded into the done set and
    skipped, ``running`` agents are re-polled (not relaunched — their window is already
    open), and ``waiting``/``ready`` agents are re-evaluated against the DAG.

    ``launch_fn`` defaults to :func:`launch_agent_window`; tests inject a stub to avoid
    opening real Terminal windows. Returns True when every agent is done, False on
    timeout or an unbreakable block (all remaining agents blocked by a failed dependency).
    """
    launch = launch_fn or launch_agent_window
    builder = DAGBuilder()

    all_ids = {a.agent_id for a in session.agents}
    done: set[str] = set()
    launched: set[str] = set()
    failed: set[str] = set()
    warned: set[str] = set()

    # Seed from persisted state (for --resume): a previously-done agent must not be
    # relaunched, and a previously-running agent must be re-polled rather than reopened.
    for agent in session.agents:
        if agent.state == "done" or is_done(session, agent):
            done.add(agent.agent_id)
        if agent.state in {"running", "done"}:
            launched.add(agent.agent_id)
        if agent.state == "failed":
            failed.add(agent.agent_id)

    deadline = None if timeout is None else time.monotonic() + timeout

    while done != all_ids:
        # 1. Promote newly-signalled agents to done.
        for agent in session.agents:
            aid = agent.agent_id
            if aid in launched and aid not in done and _agent_done(session, agent, warned):
                done.add(aid)
                update_state(session, aid, "done")

        # 2. Release agents whose dependencies are all satisfied.
        for aid in sorted(builder.ready_agents(session.dag, done)):
            if aid in launched or aid in done:
                continue
            ready_agent = session.agent_by_id(aid)
            if ready_agent is None:
                continue
            update_state(session, aid, "ready")
            launch(session, ready_agent)
            launched.add(aid)
            update_state(session, aid, "running")
            _log(f"CONDUCTOR  ● launched {aid} — dependencies satisfied; paste prompt with ⌘V")

        # 3. Mark agents whose dependency has failed (no retry) as blocked.
        for agent in session.agents:
            aid = agent.agent_id
            if aid in launched or aid in done:
                continue
            node = session.dag.get(aid)
            if node and any(dep in failed for dep in node.depends_on) and agent.state != "blocked":
                update_state(session, aid, "blocked")

        if done == all_ids:
            return True

        # Deadlock guard: nothing left can ever become ready (everything remaining is
        # blocked or has an unmet, failed dependency and no agent is still running).
        remaining = all_ids - done
        if remaining and not (remaining & launched) and not builder.ready_agents(session.dag, done):
            _log(
                "CONDUCTOR  ✗ dependency stall — remaining agents are blocked by failed "
                f"dependencies: {sorted(remaining)}",
            )
            return False

        if deadline is not None and time.monotonic() >= deadline:
            return False
        time.sleep(poll_interval)

    return True
