"""
Multi-agent commit queue — GateChain v2.

Serializes commits from concurrent agents so they do not collide on the git
index lock or on the 7-gate CI orchestrator. Agents enqueue intent (repo,
files, message); a single worker per repo drains the queue:

    wait for .git/index.lock  →  git add  →  git commit  →  call orchestrator
                                                          →  issue queue token

A queue token is the queue-layer analogue of the orchestrator's merge token:
proof that this commit was processed serially and cleared the gate chain.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shlex
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    import types

QUEUE_DIR = Path(__file__).resolve().parent
DB_PATH = QUEUE_DIR / "queue.db"
LOG_PATH = QUEUE_DIR / "queue.log"

ORCHESTRATOR_URL = os.environ.get("CDMAD_ORCHESTRATOR_URL", "http://localhost:8000/commit")
INDEX_LOCK_TIMEOUT_S = 120.0
INDEX_LOCK_POLL_S = 0.5
WORKER_IDLE_SLEEP_S = 1.0
ORCHESTRATOR_TIMEOUT_S = 600.0

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


# ── Logging ───────────────────────────────────────────────────────────────────
def get_logger() -> logging.Logger:
    """Singleton logger that writes to queue.log and stderr."""
    logger = logging.getLogger("commit_queue")
    if logger.handlers:
        return logger
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    logger.setLevel(logging.INFO)
    file_handler = logging.FileHandler(LOG_PATH)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger


# ── Database ──────────────────────────────────────────────────────────────────
def init_db() -> None:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS commits (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                repo         TEXT NOT NULL,
                agent_id     TEXT NOT NULL,
                files        TEXT NOT NULL,
                message      TEXT NOT NULL,
                status       TEXT NOT NULL,
                enqueued_at  REAL NOT NULL,
                started_at   REAL,
                finished_at  REAL,
                commit_sha   TEXT,
                merge_token  TEXT,
                queue_token  TEXT,
                error        TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_commits_repo_status "
            "ON commits(repo, status)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS queue_tokens (
                token            TEXT PRIMARY KEY,
                commit_entry_id  INTEGER NOT NULL,
                commit_sha       TEXT NOT NULL,
                issued_at        REAL NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def _normalize_repo(repo: str) -> str:
    return str(Path(repo).expanduser().resolve())


# ── Enqueue ───────────────────────────────────────────────────────────────────
def _validate_file_tokens(files: list[str]) -> None:
    """Reject file tokens that indicate a malformed --files value.

    --files is shlex-split: space-separated paths inside one quoted string. A comma in
    a token (or an empty token) almost always means the caller used comma separation or
    bad quoting, which would otherwise enqueue cleanly and then fail opaquely at
    ``git add`` in the worker with a confusing pathspec error. Catch it here, at enqueue
    time, with a message that points at the parsing rule.
    """
    for token in files:
        if not token.strip():
            raise ValueError(
                "--files contains an empty/whitespace path token — check the quoting. "
                "--files is space-separated inside one quoted string (shlex-parsed), "
                'e.g. --files "a.py b.py".'
            )
        if "," in token:
            raise ValueError(
                f"--files token {token!r} contains a comma. --files is space-separated "
                '(shlex-parsed), not comma-separated: use --files "a.py b.py", '
                'not --files "a.py,b.py". As written this would fail at git add.'
            )


def enqueue(repo: str, files: list[str], message: str, agent_id: str) -> int:
    """Insert a pending commit entry; return its row id."""
    init_db()
    repo_path = _normalize_repo(repo)
    if not (Path(repo_path) / ".git").exists():
        raise RuntimeError(f"Not a git repository: {repo_path}")
    if not files:
        raise ValueError("Must specify at least one file to commit")
    _validate_file_tokens(files)
    if not message.strip():
        raise ValueError("Commit message cannot be empty")

    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(
            "INSERT INTO commits (repo, agent_id, files, message, status, enqueued_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (repo_path, agent_id, json.dumps(files), message,
             STATUS_PENDING, time.time()),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


# ── Claim (atomic) ────────────────────────────────────────────────────────────
def _claim_next(repo_path: str) -> dict[str, Any] | None:
    """Atomically claim the oldest pending commit for `repo_path`.

    Uses BEGIN IMMEDIATE so concurrent workers serialize on the DB write lock.
    """
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT id, repo, agent_id, files, message, enqueued_at "
            "FROM commits WHERE repo = ? AND status = ? "
            "ORDER BY id ASC LIMIT 1",
            (repo_path, STATUS_PENDING),
        ).fetchone()
        if not row:
            conn.execute("COMMIT")
            return None
        entry_id = row[0]
        conn.execute(
            "UPDATE commits SET status = ?, started_at = ? WHERE id = ?",
            (STATUS_PROCESSING, time.time(), entry_id),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    return {
        "id": int(row[0]),
        "repo": str(row[1]),
        "agent_id": str(row[2]),
        "files": list(json.loads(row[3])),
        "message": str(row[4]),
        "enqueued_at": float(row[5]),
    }


# ── Git helpers ───────────────────────────────────────────────────────────────
def wait_for_index_lock(
    repo: str,
    timeout: float = INDEX_LOCK_TIMEOUT_S,
    poll: float = INDEX_LOCK_POLL_S,
) -> bool:
    """Poll until `.git/index.lock` disappears. Returns True if cleared in time."""
    lock_path = Path(repo) / ".git" / "index.lock"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not lock_path.exists():
            return True
        time.sleep(poll)
    return not lock_path.exists()


def _run_git(repo: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )


# ── Orchestrator call ─────────────────────────────────────────────────────────
def _post_to_orchestrator(agent_id: str, branch: str, commit_sha: str) -> dict[str, Any]:
    headers: dict[str, str] = {}
    gate_token = os.environ.get("CDMAD_GATE_TOKEN")
    if gate_token:
        headers["X-Gate-Token"] = gate_token
    response = httpx.post(
        ORCHESTRATOR_URL,
        json={"agent_id": agent_id, "branch": branch, "commit_sha": commit_sha},
        headers=headers,
        timeout=ORCHESTRATOR_TIMEOUT_S,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected orchestrator response: {data!r}")
    return data


# ── Token + state transitions ─────────────────────────────────────────────────
def _generate_queue_token(commit_sha: str, entry_id: int) -> str:
    seed = f"{commit_sha}|{entry_id}|{time.time()}"
    return hashlib.sha256(seed.encode()).hexdigest()[:24]


def _save_queue_token(token: str, entry_id: int, commit_sha: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO queue_tokens (token, commit_entry_id, commit_sha, issued_at) "
            "VALUES (?, ?, ?, ?)",
            (token, entry_id, commit_sha, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def _mark_done(entry_id: int, commit_sha: str, merge_token: str, queue_token: str) -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "UPDATE commits SET status = ?, commit_sha = ?, merge_token = ?, "
            "queue_token = ?, finished_at = ? WHERE id = ?",
            (STATUS_DONE, commit_sha, merge_token, queue_token, time.time(), entry_id),
        )
        conn.commit()
    finally:
        conn.close()


def _mark_failed(entry_id: int, error: str, commit_sha: str | None = None) -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "UPDATE commits SET status = ?, error = ?, commit_sha = ?, "
            "finished_at = ? WHERE id = ?",
            (STATUS_FAILED, error, commit_sha, time.time(), entry_id),
        )
        conn.commit()
    finally:
        conn.close()


# ── Worker ────────────────────────────────────────────────────────────────────
def process_entry(entry: dict[str, Any], logger: logging.Logger) -> None:
    entry_id = int(entry["id"])
    repo = str(entry["repo"])
    agent_id = str(entry["agent_id"])
    files = [str(f) for f in entry["files"]]
    message = str(entry["message"])

    logger.info("Processing #%d agent=%s files=%s", entry_id, agent_id, files)

    if not wait_for_index_lock(repo):
        err = f"timed out waiting for {repo}/.git/index.lock to clear"
        logger.error("#%d %s", entry_id, err)
        _mark_failed(entry_id, err)
        return

    add = _run_git(repo, ["add", "--", *files])
    if add.returncode != 0:
        err = f"git add failed: {add.stderr.strip() or add.stdout.strip()}"
        logger.error("#%d %s", entry_id, err)
        _mark_failed(entry_id, err)
        return

    # --no-verify because the queue itself drives the orchestrator below; the
    # pre-commit hook would otherwise call it a second time.
    commit = _run_git(repo, ["commit", "--no-verify", "-m", message])
    if commit.returncode != 0:
        err = f"git commit failed: {commit.stderr.strip() or commit.stdout.strip()}"
        logger.error("#%d %s", entry_id, err)
        _mark_failed(entry_id, err)
        return

    sha_proc = _run_git(repo, ["rev-parse", "HEAD"])
    branch_proc = _run_git(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    commit_sha = sha_proc.stdout.strip()
    branch = branch_proc.stdout.strip() or "unknown"

    logger.info("#%d submitted sha=%s branch=%s", entry_id, commit_sha[:8], branch)
    try:
        result = _post_to_orchestrator(agent_id, branch, commit_sha)
    except (httpx.HTTPError, RuntimeError) as e:
        err = f"orchestrator call failed: {e}"
        logger.error("#%d %s", entry_id, err)
        _mark_failed(entry_id, err, commit_sha=commit_sha)
        return

    merge_token = result.get("merge_token")
    if not merge_token:
        blocked = result.get("blocked_at") or "unknown"
        reason_head = (result.get("reason") or "").splitlines()[:5]
        err = f"gate blocked at '{blocked}': {' | '.join(reason_head)}"
        logger.error("#%d %s", entry_id, err)
        _mark_failed(entry_id, err, commit_sha=commit_sha)
        return

    queue_token = _generate_queue_token(commit_sha, entry_id)
    _save_queue_token(queue_token, entry_id, commit_sha)
    _mark_done(entry_id, commit_sha, str(merge_token), queue_token)
    logger.info(
        "#%d DONE sha=%s merge_token=%s queue_token=%s",
        entry_id, commit_sha[:8], merge_token, queue_token,
    )


_shutdown_requested = False


def _install_signal_handlers() -> None:
    def handler(_signum: int, _frame: types.FrameType | None) -> None:
        global _shutdown_requested
        _shutdown_requested = True

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def worker(repo: str) -> None:
    init_db()
    logger = get_logger()
    repo_path = _normalize_repo(repo)
    if not (Path(repo_path) / ".git").exists():
        raise RuntimeError(f"Not a git repository: {repo_path}")

    _install_signal_handlers()
    logger.info("Worker started repo=%s pid=%d", repo_path, os.getpid())

    while not _shutdown_requested:
        try:
            entry = _claim_next(repo_path)
        except sqlite3.OperationalError as e:
            logger.warning("claim contention: %s — retrying", e)
            time.sleep(WORKER_IDLE_SLEEP_S)
            continue

        if entry is None:
            time.sleep(WORKER_IDLE_SLEEP_S)
            continue

        try:
            process_entry(entry, logger)
        except Exception as e:  # noqa: BLE001
            logger.exception("unhandled error processing #%d", entry["id"])
            _mark_failed(int(entry["id"]), f"worker exception: {e}")

    logger.info("Worker stopped repo=%s", repo_path)


# ── Status ────────────────────────────────────────────────────────────────────
def status(repo: str) -> dict[str, Any]:
    init_db()
    repo_path = _normalize_repo(repo)
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM commits WHERE repo = ? GROUP BY status",
            (repo_path,),
        ).fetchall()
        counts = {s: 0 for s in (STATUS_PENDING, STATUS_PROCESSING, STATUS_DONE, STATUS_FAILED)}
        for s, c in rows:
            counts[str(s)] = int(c)

        recent = conn.execute(
            "SELECT id, agent_id, status, message, commit_sha, queue_token, "
            "enqueued_at, error FROM commits WHERE repo = ? "
            "ORDER BY id DESC LIMIT 10",
            (repo_path,),
        ).fetchall()
    finally:
        conn.close()

    return {
        "repo": repo_path,
        "counts": counts,
        "recent": [
            {
                "id": int(r[0]),
                "agent_id": str(r[1]),
                "status": str(r[2]),
                "message": str(r[3]),
                "commit_sha": r[4],
                "queue_token": r[5],
                "enqueued_at": float(r[6]),
                "error": r[7],
            }
            for r in recent
        ],
    }


def _print_status(s: dict[str, Any]) -> None:
    print(f"\nQueue status for {s['repo']}")
    print("─" * 72)
    c = s["counts"]
    print(f"  pending:    {c[STATUS_PENDING]}")
    print(f"  processing: {c[STATUS_PROCESSING]}")
    print(f"  done:       {c[STATUS_DONE]}")
    print(f"  failed:     {c[STATUS_FAILED]}")
    print("─" * 72)
    if s["recent"]:
        print("  Recent (newest first):")
        for r in s["recent"]:
            sha = (r["commit_sha"] or "—")[:8]
            tok = (r["queue_token"] or "—")[:12]
            msg = r["message"].splitlines()[0][:40]
            print(
                f"  #{r['id']:<4} [{r['status']:<10}] "
                f"{r['agent_id']:<14} {sha:<8} {tok:<12} {msg}"
            )
            if r["status"] == STATUS_FAILED and r["error"]:
                print(f"        error: {r['error'][:100]}")
    print()


# ── CLI ───────────────────────────────────────────────────────────────────────
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="commit_queue",
        description="Multi-agent commit queue — GateChain v2",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_enq = sub.add_parser("enqueue", help="Enqueue a commit")
    p_enq.add_argument("--repo", required=True)
    p_enq.add_argument(
        "--files",
        required=True,
        action="append",
        metavar="FILES",
        help=(
            'Files to commit, space-separated inside one quoted string (shlex-parsed), '
            'e.g. --files "a.py b.py". Repeatable: pass --files more than once and the '
            "values are concatenated. Use spaces, not commas."
        ),
    )
    p_enq.add_argument("--message", required=True)
    p_enq.add_argument(
        "--agent-id",
        default=os.environ.get("CI_AGENT_ID", "agent-unknown"),
    )

    p_work = sub.add_parser("worker", help="Run the queue worker for a repo")
    p_work.add_argument("--repo", required=True)

    p_stat = sub.add_parser("status", help="Show queue status for a repo")
    p_stat.add_argument("--repo", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.cmd == "enqueue":
        # args.files is a list of --files values (action="append"); shlex-split each and
        # concatenate so repeated flags accumulate instead of silently keeping the last.
        files = [tok for chunk in args.files for tok in shlex.split(chunk)]
        entry_id = enqueue(args.repo, files, args.message, args.agent_id)
        print(f"enqueued #{entry_id} repo={_normalize_repo(args.repo)} files={files}")
        return 0
    if args.cmd == "worker":
        worker(args.repo)
        return 0
    if args.cmd == "status":
        _print_status(status(args.repo))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
