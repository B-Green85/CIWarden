#!/usr/bin/env python3
"""Append one JSON line to ``.cdmad/commit_log.jsonl`` after a gated commit.

Mechanical data only — git metadata + the orchestrator's gate-run audit trail
(``orchestrator/gate_results.db``). Best-effort: never raises in a way that could
block a commit. stdlib only.

Usage:
    python3 scripts/update_commit_log.py [COMMIT_SHA]

If no SHA is given, uses ``git rev-parse HEAD``.

Gate correlation: the pre-commit hook gates the parent SHA (HEAD before the new
commit object exists), so a post-commit HEAD won't exact-match the DB. We try an
exact SHA match first, then fall back to the most-recent gate run (the one that
just passed). If the DB has no runs at all, ``token``/``gates`` are written null.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
REPO_DIR = ROOT
DB_PATH = ROOT / "orchestrator" / "gate_results.db"
LOG_PATH = ROOT / ".cdmad" / "commit_log.jsonl"

GATE_NAMES = ("lint", "typecheck", "security", "memory", "test", "stress", "build")
_SHORTSTAT = re.compile(
    r"(\d+) files? changed(?:, (\d+) insertions?\(\+\))?(?:, (\d+) deletions?\(-\))?"
)


# ── git ──────────────────────────────────────────────────────────


def _git(*args: str) -> str:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(REPO_DIR), *args], stderr=subprocess.DEVNULL
        )
        return out.decode().strip()
    except Exception:
        return ""


def _resolve_sha(argv: list[str]) -> str:
    for arg in argv[1:]:
        if arg and not arg.startswith("-"):
            return arg
    return _git("rev-parse", "HEAD")


def _diffstat(sha: str) -> tuple[int, int, int]:
    out = _git("show", sha, "--shortstat", "--format=")
    m = _SHORTSTAT.search(out)
    if not m:
        return 0, 0, 0
    return int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0)


# ── audit db (orchestrator/gate_results.db) ──────────────────────


def _gates_for_sha(conn: sqlite3.Connection, sha: str) -> dict[str, dict[str, Any]]:
    """Latest row per gate for a commit SHA → {gate: {status, ms}}."""
    gates: dict[str, dict[str, Any]] = {}
    for name in GATE_NAMES:
        row = conn.execute(
            "SELECT status, duration_ms FROM gate_runs "
            "WHERE commit_sha = ? AND gate_name = ? ORDER BY timestamp DESC LIMIT 1",
            (sha, name),
        ).fetchone()
        if row is not None:
            gates[name] = {"status": str(row[0]).upper(), "ms": int(row[1])}
    return gates


def _agent_for_sha(conn: sqlite3.Connection, sha: str) -> str | None:
    row = conn.execute(
        "SELECT agent_id FROM gate_runs WHERE commit_sha = ? ORDER BY timestamp DESC LIMIT 1",
        (sha,),
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def _token_for_sha(conn: sqlite3.Connection, sha: str) -> str | None:
    row = conn.execute(
        "SELECT token FROM merge_tokens WHERE commit_sha = ? ORDER BY issued_at DESC LIMIT 1",
        (sha,),
    ).fetchone()
    return str(row[0]) if row else None


def _gate_fields(sha: str) -> tuple[str | None, dict[str, Any] | None, int | None, str | None]:
    """Return (token, gates, total_ms, agent_id). All-None when no run is found."""
    if not Path(DB_PATH).exists():
        return None, None, None, None
    conn = sqlite3.connect(str(DB_PATH))
    try:
        run_sha = sha
        gates = _gates_for_sha(conn, sha)
        if not gates:
            # Recency fallback — the run that just passed (keyed by the parent SHA).
            row = conn.execute(
                "SELECT commit_sha FROM gate_runs ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return None, None, None, None
            run_sha = str(row[0])
            gates = _gates_for_sha(conn, run_sha)
        if not gates:
            return None, None, None, None
        total_ms = sum(int(g["ms"]) for g in gates.values())
        return _token_for_sha(conn, run_sha), gates, total_ms, _agent_for_sha(conn, run_sha)
    finally:
        conn.close()


# ── entry assembly ───────────────────────────────────────────────


def build_entry(sha: str) -> dict[str, Any]:
    files_changed, insertions, deletions = _diffstat(sha)
    token, gates, total_ms, db_author = _gate_fields(sha)
    # author: prefer the gate-run agent_id (what the system recorded), else git author.
    author = db_author or _git("show", "-s", "--format=%an", sha)
    return {
        "sha": sha,
        "short_sha": sha[:7],
        "token": token,
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "message": _git("show", "-s", "--format=%s", sha),
        "author": author,
        "files_changed": files_changed,
        "insertions": insertions,
        "deletions": deletions,
        "gates": gates,
        "total_ms": total_ms,
        "consumed": False,
        "timestamp": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _existing_shas(log_path: Path) -> set[str]:
    if not log_path.exists():
        return set()
    shas: set[str] = set()
    for line in log_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            shas.add(json.loads(line).get("sha"))
        except json.JSONDecodeError:
            continue
    return shas


def append_entry(sha: str) -> bool:
    """Append a commit-log line for ``sha``. Returns True if written, False if skipped."""
    log_path = Path(LOG_PATH)
    if not sha:
        return False
    if sha in _existing_shas(log_path):
        return False  # idempotent — already logged
    entry = build_entry(sha)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    return True


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    try:
        sha = _resolve_sha(argv)
        wrote = append_entry(sha)
        if wrote:
            print(f"commit_log: appended {sha[:7]}")
        else:
            print(f"commit_log: {sha[:7] if sha else '(no sha)'} skipped (already logged or no HEAD)")
    except Exception as e:  # best-effort — never fatal
        print(f"commit_log: skipped ({e})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
