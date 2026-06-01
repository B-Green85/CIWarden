#!/usr/bin/env python3
"""Synthesize a DEVLOG.md skeleton from unconsumed commit-log entries.

Reads ``.cdmad/commit_log.jsonl``, filters to ``consumed: false`` entries, prints a
mechanical summary, appends a skeleton entry to ``DEVLOG.md`` (with a NARRATIVE
placeholder), and marks the entries consumed. It does NOT write the narrative —
that is Claude Code's job after reading the summary. stdlib only.

Usage:
    python3 scripts/write_devlog_entry.py            # write skeleton for unconsumed commits
    python3 scripts/write_devlog_entry.py --since 2026-05-31
    python3 scripts/write_devlog_entry.py --dry-run  # print skeleton, write nothing
    python3 scripts/write_devlog_entry.py --list      # list unconsumed, write nothing
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / ".cdmad" / "commit_log.jsonl"
DEVLOG_PATH = ROOT / "DEVLOG.md"

GATE_NAMES = ("lint", "typecheck", "security", "memory", "test", "stress", "build")
EPIGRAPH = '*"Generation is optional. Verification is not."*'


# ── jsonl I/O ────────────────────────────────────────────────────


def load_entries(log_path: Path) -> list[dict[str, Any]]:
    if not log_path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in log_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def unconsumed_entries(entries: list[dict[str, Any]], since: str | None = None) -> list[dict[str, Any]]:
    out = [e for e in entries if not e.get("consumed")]
    if since:
        out = [e for e in out if str(e.get("timestamp", ""))[:10] >= since]
    return out


def mark_consumed(log_path: Path, shas: set[str]) -> None:
    """Rewrite the jsonl with ``consumed: true`` on the given SHAs (order preserved)."""
    if not log_path.exists():
        return
    lines_out: list[str] = []
    for line in log_path.read_text().splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
        except json.JSONDecodeError:
            lines_out.append(line)
            continue
        if entry.get("sha") in shas:
            entry["consumed"] = True
        lines_out.append(json.dumps(entry))
    log_path.write_text("\n".join(lines_out) + "\n")


# ── derived data ─────────────────────────────────────────────────


def _gate_marker(entry: dict[str, Any]) -> str:
    gates = entry.get("gates")
    if not isinstance(gates, dict) or not gates:
        return "—"
    total = len(gates)
    passed = sum(1 for g in gates.values() if str(g.get("status")).upper() == "PASS")
    icon = "✓" if passed == total else "✗"
    return f"{icon} {passed}/{total}"


def gate_summary(entries: list[dict[str, Any]]) -> dict[str, tuple[int, int, int]]:
    """Per gate → (fastest_ms, slowest_ms, runs) across entries that recorded it."""
    summary: dict[str, tuple[int, int, int]] = {}
    for name in GATE_NAMES:
        times = [
            int(e["gates"][name]["ms"])
            for e in entries
            if isinstance(e.get("gates"), dict) and name in e["gates"]
        ]
        if times:
            summary[name] = (min(times), max(times), len(times))
    return summary


def build_summary(entries: list[dict[str, Any]]) -> str:
    ins = sum(int(e.get("insertions") or 0) for e in entries)
    dels = sum(int(e.get("deletions") or 0) for e in entries)
    lines = [
        f"Unconsumed commits: {len(entries)}",
        f"Total lines changed: +{ins} / -{dels}",
        "",
        "Commits:",
    ]
    for e in entries:
        total = e.get("total_ms")
        total_str = f"{total}ms" if total is not None else "—"
        lines.append(f"  {e.get('short_sha', '???????')}  {_gate_marker(e)}  {total_str}  {e.get('message', '')}")
    lines.append("")
    lines.append("Gate timings (fastest / slowest / runs):")
    for name, (lo, hi, n) in gate_summary(entries).items():
        lines.append(f"  {name:<10} {lo}ms / {hi}ms / {n}")
    tokens = [str(e["token"]) for e in entries if e.get("token")]
    if tokens:
        lines.append("")
        lines.append("Merge tokens:")
        lines.extend(f"  {t}" for t in tokens)
    return "\n".join(lines)


def build_skeleton(entries: list[dict[str, Any]], date: str) -> str:
    rows = []
    for e in entries:
        total = e.get("total_ms")
        total_str = f"{total}ms" if total is not None else "—"
        rows.append(f"| `{e.get('short_sha', '')}` | {e.get('message', '')} | {_gate_marker(e)} | {total_str} |")

    gate_rows = []
    summary = gate_summary(entries)
    for name in GATE_NAMES:
        if name in summary:
            lo, hi, n = summary[name]
            gate_rows.append(f"| {name} | {lo}ms | {hi}ms | {n} |")
        else:
            gate_rows.append(f"| {name} | — | — | 0 |")

    tokens = [str(e["token"]) for e in entries if e.get("token")] or ["(none)"]

    return "\n".join([
        f"## {date} — {{title placeholder}}",
        "",
        "<!-- NARRATIVE: Replace this block with session narrative -->",
        "",
        "---",
        "",
        "### Commits This Session",
        "",
        "| SHA | Message | Gates | Total |",
        "|-----|---------|-------|-------|",
        *rows,
        "",
        "---",
        "",
        "### Gate Summary",
        "",
        "| Gate | Fastest | Slowest | Runs |",
        "|------|---------|---------|------|",
        *gate_rows,
        "",
        "---",
        "",
        "### Merge Tokens",
        "",
        *tokens,
        "",
        "---",
        "",
        EPIGRAPH,
    ])


def append_devlog(devlog_path: Path, skeleton: str) -> None:
    if devlog_path.exists() and devlog_path.read_text().strip():
        existing = devlog_path.read_text().rstrip()
        devlog_path.write_text(f"{existing}\n\n---\n\n{skeleton}\n")
    else:
        devlog_path.write_text(skeleton + "\n")


# ── CLI ──────────────────────────────────────────────────────────


def _today() -> str:
    dt = datetime.now(UTC)
    return f"{dt:%B} {dt.day}, {dt.year}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="write_devlog_entry",
        description="Write a DEVLOG.md skeleton from unconsumed commits",
    )
    parser.add_argument("--since", default=None, help="Only include commits on/after this date (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true", help="Print the skeleton, write nothing")
    parser.add_argument("--list", action="store_true", dest="list_only", help="List unconsumed commits, write nothing")
    args = parser.parse_args(argv)

    entries = unconsumed_entries(load_entries(Path(LOG_PATH)), since=args.since)

    if not entries:
        print("No unconsumed commits. Nothing to write.")
        return 0

    if args.list_only:
        print(build_summary(entries))
        return 0

    skeleton = build_skeleton(entries, _today())

    if args.dry_run:
        print(build_summary(entries))
        print()
        print(skeleton)
        return 0

    # Normal run: print the mechanical summary (for the narrator), write the
    # skeleton, and mark the entries consumed. Narrative is added afterward.
    print(build_summary(entries))
    append_devlog(Path(DEVLOG_PATH), skeleton)
    mark_consumed(Path(LOG_PATH), {str(e["sha"]) for e in entries})
    print(f"\nWrote skeleton to {DEVLOG_PATH} and marked {len(entries)} commit(s) consumed.")
    print("Replace the <!-- NARRATIVE --> placeholder and {title placeholder} with the session narrative.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
