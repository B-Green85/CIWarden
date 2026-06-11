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

import atexit
import contextlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from conductor.dag import DAGBuilder

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import FrameType

    from conductor.session import AgentSpec, ConductorSession

DONE_FLAG = ".done"
CONFLICT_REPORT_FILE = ".conflict_report.txt"
SCHEMA_TEMPLATE = ".cdmad/session_schema.json"
LAUNCH_SCRIPT = "launch_agents.sh"
PROMPT_FILE = "PROMPT.md"


def _log(message: str) -> None:
    print(message)


# ── Repo isolation (read-only target repos during a session) ──────
#
# GOVERNANCE INVARIANT: while agents are running, the target repo(s) are the staging
# dirs' read-only mirror — every byte of agent output MUST reach the repo through the
# Conductor's atomic commit, never by an agent writing the worktree directly. We enforce
# this by stripping the write bit off every file and directory in each repo for the
# duration of the agent phase, then restoring each path's exact original mode.
#
# Restore TIMING is dictated by a hard mechanical fact: the atomic commit copies staged
# files INTO the worktree (conductor.commit.copy_staged_to_worktree → shutil.copy2 +
# mkdir). A recursively read-only repo (chmod -R a-w strips write on *directories* too,
# blocking new-file creation) would make that copy raise PermissionError. So the
# functional restore happens the instant the agent phase ends — the last hook this module
# owns before cli.run_session calls atomic_commit — leaving the repo writable for proof +
# commit. The atexit / signal cleanup handlers are the bulletproof net: they restore on
# normal exit (firing AFTER the commit, at interpreter teardown), on an unhandled
# exception, and on SIGTERM / SIGINT — so a crashed Conductor never leaves a repo locked.
#
# Repo paths come from the same registry the RepoRouter is built from: session.repo_root
# (the default repo) plus the wizard's repo_paths.json sidecar (every additional repo in a
# multi-repo session). Mirrors conductor.cli.REPO_PATHS_FILE — duplicated, not imported,
# to avoid a cli ↔ distributor import cycle.
REPO_PATHS_FILE = "repo_paths.json"

# Write bits for owner/group/other; isolation clears exactly these and nothing else, so
# read and execute permissions are preserved (directories stay traversable, scripts stay
# runnable). Restoration writes back the full saved st_mode, so any bit we touched returns.
_WRITE_BITS = 0o222


def _iter_repo_paths(root: Path) -> list[str]:
    """Every non-symlink file and directory under ``root`` (root included), as strings.

    Symlinks are skipped so we never chmod a link's target — which could live outside the
    repo. ``os.walk`` does not descend into symlinked directories (followlinks=False), so
    isolation stays contained within the repo tree.
    """
    paths: list[str] = []
    root_str = str(root)
    if not os.path.islink(root_str):
        paths.append(root_str)
    for dirpath, dirnames, filenames in os.walk(root_str, followlinks=False):
        for name in (*dirnames, *filenames):
            candidate = os.path.join(dirpath, name)
            if not os.path.islink(candidate):
                paths.append(candidate)
    return paths


def _session_repo_paths(session: ConductorSession) -> list[Path]:
    """Resolved roots of every repo in the session — default repo plus sidecar repos.

    The default repo is always ``session.repo_root``. For a multi-repo session the wizard
    persisted each additional repo's path in ``repo_paths.json`` next to the manifest; we
    read it here so isolation covers ALL repos an agent might write to, not just the
    invoking one. De-duplicated by resolved path so the same repo under two spellings (or
    an override that equals the default) is isolated once.
    """
    roots: list[Path] = [session.repo_root]
    if getattr(session, "multi_repo", False):
        sidecar = session.staging_base / REPO_PATHS_FILE
        if sidecar.exists():
            try:
                data = json.loads(sidecar.read_text())
            except (json.JSONDecodeError, OSError):
                data = {}
            if isinstance(data, dict):
                roots.extend(Path(str(v)).expanduser() for v in data.values())
    seen: set[str] = set()
    unique: list[Path] = []
    for root in roots:
        try:
            resolved = root.expanduser().resolve()
        except OSError:
            continue
        key = str(resolved)
        if key not in seen and resolved.is_dir():
            seen.add(key)
            unique.append(resolved)
    return unique


class _RepoIsolator:
    """Makes a set of repos read-only for the agent phase and restores them exactly.

    A single module-level instance (:data:`_ISOLATOR`) holds the original-mode map so the
    two non-DAG hooks — ``write_launch_script`` isolates, ``wait_for_done`` restores — and
    the DAG hook (``run_dependency_aware`` does both in a try/finally) share one snapshot.
    Restoration is idempotent: whichever of the functional restore, atexit, or a signal
    handler fires first wins; the rest are no-ops.
    """

    def __init__(self) -> None:
        self._original_modes: dict[str, int] = {}
        self._isolated_repos: list[str] = []
        self._active = False
        self._cleanup_registered = False
        self._prev_handlers: dict[int, Any] = {}

    def isolate(self, repos: list[Path]) -> None:
        """Snapshot then strip write bits from every path under each repo.

        No-op if already active (re-isolating would snapshot already-read-only modes as the
        "original", corrupting restore) or if there is nothing to isolate. The cleanup
        handler is registered BEFORE the first chmod, so even a crash mid-isolation
        restores whatever was already changed.
        """
        if self._active or not repos:
            return
        # Register cleanup BEFORE any permission change — the whole point is that a crash
        # at any later point still restores.
        self._register_cleanup()
        self._active = True
        self._original_modes.clear()
        self._isolated_repos.clear()
        for repo in repos:
            paths = _iter_repo_paths(repo)
            for path in paths:
                try:
                    self._original_modes[path] = os.stat(path).st_mode & 0o7777
                except OSError:
                    continue
            for path, mode in list(self._original_modes.items()):
                with contextlib.suppress(OSError):
                    os.chmod(path, mode & ~_WRITE_BITS)
            self._isolated_repos.append(str(repo))
            _log(f"CONDUCTOR  ● repo isolated: {repo} (write permissions removed)")

    def restore(self) -> None:
        """Restore every path's exact original mode. Idempotent; safe from any context."""
        if not self._active:
            return
        self._active = False
        for path, mode in self._original_modes.items():
            with contextlib.suppress(OSError):
                os.chmod(path, mode)
        for repo in self._isolated_repos:
            _log(f"CONDUCTOR  ● repo restored: {repo} (write permissions restored)")
        self._original_modes.clear()
        self._isolated_repos.clear()

    def _register_cleanup(self) -> None:
        """Wire atexit + SIGTERM/SIGINT to restore — once, before any chmod.

        ``atexit`` covers a normal return (firing after atomic_commit at interpreter
        teardown) and an unhandled exception. SIGTERM and SIGINT need explicit handlers:
        SIGTERM's default termination skips atexit entirely, so without a handler a
        ``kill`` would leave the repo locked. Signal registration that fails (e.g. called
        off the main thread, as some tests do) is tolerated — atexit still guards the
        common paths.
        """
        if self._cleanup_registered:
            return
        self._cleanup_registered = True
        atexit.register(self.restore)
        for sig in (signal.SIGTERM, signal.SIGINT):
            # Not the main thread / unsupported platform — atexit remains in force.
            with contextlib.suppress(ValueError, OSError):
                self._prev_handlers[sig] = signal.signal(sig, self._handle_signal)

    def _handle_signal(self, signum: int, frame: FrameType | None) -> None:
        """Restore permissions, then re-raise the signal under its previous disposition.

        We do not swallow the signal: after restoring we reinstate the prior handler and
        re-send the signal so the process still terminates (or runs whatever handler was
        installed before us). This keeps Ctrl-C / kill behaving normally while guaranteeing
        the repo is unlocked first.
        """
        self.restore()
        prev = self._prev_handlers.get(signum, signal.SIG_DFL)
        if callable(prev):
            prev(signum, frame)
            return
        try:
            signal.signal(signum, prev)
        except (ValueError, OSError):
            signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)


_ISOLATOR = _RepoIsolator()


def isolate_session_repos(session: ConductorSession) -> None:
    """Make every repo in the session read-only for the agent phase (write bits removed)."""
    _ISOLATOR.isolate(_session_repo_paths(session))


def restore_session_repos() -> None:
    """Restore the original permissions of every isolated repo. Idempotent."""
    _ISOLATOR.restore()


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
    # Lock the repo(s) read-only BEFORE the operator runs this script and opens any agent
    # window. wait_for_done restores once every agent has signalled. (The script and
    # staging dirs live outside the repo, so isolating here does not block writing them.)
    isolate_session_repos(session)
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

    Restores any repo isolation applied by :func:`write_launch_script` once every agent has
    signalled (or on timeout), so the repo is writable again for proofing and the atomic
    commit. The restore is in a ``finally`` — a poll-loop exception can never leave a repo
    locked, and the atexit/signal handlers back it up regardless.
    """
    try:
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
    finally:
        restore_session_repos()


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

    The repo(s) are isolated read-only before the first window opens and restored in a
    ``finally`` once the loop ends (all done, timeout, or stall) — so the repo is writable
    again for proofing and the atomic commit, and an exception in the loop never leaves it
    locked. The atexit/signal handlers back this up on any abnormal exit.
    """
    isolate_session_repos(session)
    try:
        return _run_dependency_aware(
            session, launch_fn=launch_fn, poll_interval=poll_interval, timeout=timeout,
        )
    finally:
        restore_session_repos()


def _run_dependency_aware(
    session: ConductorSession,
    *,
    launch_fn: Callable[[ConductorSession, AgentSpec], None] | None = None,
    poll_interval: float = 1.0,
    timeout: float | None = None,
) -> bool:
    """The release loop proper. Isolation is owned by :func:`run_dependency_aware`."""
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
