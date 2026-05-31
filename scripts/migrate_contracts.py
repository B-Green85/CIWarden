"""One-time migration: JSON ContractStore → JSON VDB (VectorContractStore layout).

Non-destructive — the original ``.cdmad/contracts/`` tree is archived, not deleted,
and the migration is idempotent (a second run with nothing left to migrate is a no-op).

    python3 scripts/migrate_contracts.py
    python3 scripts/migrate_contracts.py --repo ci-wrapper
    python3 scripts/migrate_contracts.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conductor.vdb_io import _get_repo_name  # noqa: E402
from memory.contract_store import ContractStore  # noqa: E402

if TYPE_CHECKING:
    from memory.models import ContractSummary


def _session_of(generation_id: str) -> str:
    """Extract the session id from a generation id (``gen_{sid}_{seq}``)."""
    if generation_id.startswith("gen_"):
        return generation_id[4:].rsplit("_", 1)[0]
    return generation_id.rsplit("_", 1)[0] if "_" in generation_id else generation_id


def plan_migration(store: ContractStore) -> dict[str, dict[str, ContractSummary]]:
    """Group contracts by session id; keep the latest summary per module per session."""
    by_session: dict[str, dict[str, ContractSummary]] = {}
    for module in store.list_modules():
        for summary in store.list_contracts(module):
            sid = _session_of(summary.generation_id)
            modules = by_session.setdefault(sid, {})
            prev = modules.get(module)
            if prev is None or summary.sequence >= prev.sequence:
                modules[module] = summary
    return by_session


def _ordered_sessions(plan: dict[str, dict[str, ContractSummary]]) -> list[str]:
    """Session ids ordered by their max sequence (ascending → last is latest)."""
    def max_seq(sid: str) -> int:
        return max((s.sequence for s in plan[sid].values()), default=0)

    return sorted(plan, key=max_seq)


def run_migration(
    root: str | Path = ".cdmad",
    repo_name: str | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    root_path = Path(root)
    repo = repo_name or _get_repo_name()
    store = ContractStore(root=root_path)
    plan = plan_migration(store)

    receipt: dict[str, Any] = {
        "repo": repo,
        "sessions": {sid: sorted(mods) for sid, mods in plan.items()},
        "session_count": len(plan),
        "module_count": sum(len(m) for m in plan.values()),
        "dry_run": dry_run,
        "archived": False,
    }

    if not plan:
        print("migrate_contracts: nothing to migrate (no contracts found)")
        return receipt

    ordered = _ordered_sessions(plan)
    vdb_repo_dir = root_path / "vdb" / repo

    print(f"migrate_contracts: {receipt['module_count']} contracts across {len(plan)} session(s) → {vdb_repo_dir}")
    for sid in ordered:
        print(f"  session {sid}: {', '.join(sorted(plan[sid]))}")

    if dry_run:
        print("migrate_contracts: --dry-run, no files written")
        return receipt

    vdb_repo_dir.mkdir(parents=True, exist_ok=True)
    for sid in ordered:
        modules = plan[sid]
        seq = max((s.sequence for s in modules.values()), default=0)
        gen_id = next(iter(modules.values())).generation_id if modules else f"gen_{sid}"
        payload = {
            "session_id": sid,
            "sequence": seq,
            "generation_id": gen_id,
            "modules": {name: summary.model_dump(mode="json") for name, summary in modules.items()},
        }
        (vdb_repo_dir / f"session_{sid}.json").write_text(json.dumps(payload, indent=2, default=str))

    index: dict[str, str] = {"latest_session": ordered[-1]}
    if len(ordered) >= 2:
        index["previous_session"] = ordered[-2]
    (vdb_repo_dir / "index.json").write_text(json.dumps(index, indent=2))

    # Archive the original contracts tree (non-destructive).
    contracts_dir = root_path / "contracts"
    archive_dir = root_path / "contracts_archive"
    if contracts_dir.exists() and not archive_dir.exists():
        shutil.move(str(contracts_dir), str(archive_dir))
        receipt["archived"] = True

    receipt["index"] = index
    receipt_path = root_path / f"migration_{int(time.time())}.json"
    receipt_path.write_text(json.dumps(receipt, indent=2, default=str))
    receipt["receipt_path"] = str(receipt_path)
    print(f"migrate_contracts: done — index {index}, receipt {receipt_path}")
    return receipt


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="migrate_contracts", description="Migrate JSON ContractStore → VDB")
    parser.add_argument("--repo", default=None, help="Repo name for VDB scoping (default: auto from git)")
    parser.add_argument("--root", default=".cdmad", help="CDMAD root directory (default: .cdmad)")
    parser.add_argument("--dry-run", action="store_true", help="Show the plan, write nothing")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    run_migration(root=args.root, repo_name=args.repo, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
