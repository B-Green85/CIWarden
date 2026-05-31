"""Standalone VDB primer — index an existing codebase into the VDB.

A thin CLI wrapper around ``conductor.vdb_io.prime_vdb_from_codebase`` (same logic the
Conductor runs at startup). Use for auditing a repo without running agents, resetting a
VDB baseline after a refactor, or keeping the VDB current in CI.

    python3 scripts/prime_vdb.py
    python3 scripts/prime_vdb.py --repo GolemLinux
    python3 scripts/prime_vdb.py --vdb-path /shared/cdmad/vdb
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Allow running as a bare script (python3 scripts/prime_vdb.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conductor.session import ConductorSession  # noqa: E402
from conductor.vdb_io import _get_repo_name, prime_vdb_from_codebase  # noqa: E402


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prime_vdb", description="Prime the VDB from an existing codebase")
    parser.add_argument("--repo", default=None, help="Override repo name for scoping (default: auto from git)")
    parser.add_argument("--vdb-path", default=".cdmad/vdb", help="VDB root directory (default: .cdmad/vdb)")
    parser.add_argument("--repo-root", default=".", help="Path to the repo to scan (default: cwd)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    repo_name = args.repo or _get_repo_name(repo_root)
    session = ConductorSession(
        repo_root=repo_root,
        repo_name=repo_name,
        agents=[],
        vdb_root=Path(args.vdb_path),
    )
    asyncio.run(prime_vdb_from_codebase(session))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
