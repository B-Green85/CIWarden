"""VDB I/O for the Conductor — priming, index reads, and session writes.

Shared with ``scripts/prime_vdb.py`` (a thin CLI wrapper around
``prime_vdb_from_codebase``). ``_get_repo_name`` lives in ``memory.schema_capture``
(the lower layer); it is re-exported here so callers can import it from either place
without creating a memory→conductor dependency.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from memory.contract_store import VectorContractStore
from memory.schema_capture import SchemaCaptureClient, _get_repo_name

if TYPE_CHECKING:
    from conductor.session import ConductorSession
    from memory.models import ContractSummary

__all__ = ["load_vdb_index", "prime_vdb_from_codebase", "write_session_vdb", "_get_repo_name"]

# Source files the primer/discovery considers. Python + Rust for now.
_SOURCE_SUFFIXES = (".py", ".rs")
_EXCLUDED_DIRS = {
    "tests", "__pycache__", ".venv", "venv", ".cdmad", ".git",
    "node_modules", "target", "build", ".mypy_cache", ".pytest_cache",
    ".ruff_cache",
}


def _log(message: str) -> None:
    print(message)


def store_for(session: ConductorSession) -> VectorContractStore:
    """Build a VectorContractStore bound to this session's repo + id."""
    return VectorContractStore(
        repo_name=session.repo_name,
        vdb_root=session.vdb_root,
        session_id=session.session_id,
    )


def load_vdb_index(session: ConductorSession) -> dict[str, Any]:
    """Read the VDB index for this session's repo (empty dict if absent)."""
    index_file = session.repo_vdb_dir / "index.json"
    if not index_file.exists():
        return {}
    try:
        data = json.loads(index_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def discover_source_files(repo_root: str | Path = ".") -> dict[str, str]:
    """Discover source files grouped by top-level module, concatenated per module.

    Mirrors the memory gate's discovery but also includes Rust ``.rs`` files so the
    primer can index foreign (e.g. kernel) repos.
    """
    root_path = Path(repo_root).resolve()
    modules: dict[str, list[Path]] = {}

    for path in sorted(root_path.rglob("*")):
        if path.suffix not in _SOURCE_SUFFIXES or not path.is_file():
            continue
        rel = path.relative_to(root_path)
        if any(part in _EXCLUDED_DIRS for part in rel.parts):
            continue
        if rel.name.startswith("."):
            continue
        module_name = rel.parts[0].removesuffix(path.suffix) if len(rel.parts) == 1 else rel.parts[0]
        modules.setdefault(module_name, []).append(path)

    result: dict[str, str] = {}
    for module_name, files in modules.items():
        chunks = [f"# --- {f.relative_to(root_path)} ---\n{f.read_text()}" for f in files]
        result[module_name] = "\n\n".join(chunks)
    return result


def write_session_vdb(session: ConductorSession, contracts: dict[str, Any] | list[ContractSummary]) -> None:
    """Commit a contract corpus to the VDB as one atomic session."""
    store_for(session).commit_session(contracts)


async def prime_vdb_from_codebase(session: ConductorSession) -> None:
    """Scan the existing repo and write contracts to the VDB as the baseline session.

    Skipped if the VDB already has a committed session for this repo (warm start).
    """
    index = load_vdb_index(session)
    if index.get("latest_session"):
        _log("CONDUCTOR  ● VDB already primed — skipping codebase scan")
        return

    _log("CONDUCTOR  ● priming VDB from existing codebase...")
    source_files = discover_source_files(session.repo_root)
    client = SchemaCaptureClient(repo_name=session.repo_name)
    summaries: list[ContractSummary] = []
    for module, code in sorted(source_files.items()):
        summaries.append(await client.extract_contracts(code, module, "prime_000", 0))
    store_for(session).commit_session(summaries)
    _log(f"CONDUCTOR  ✓ VDB primed — {len(summaries)} modules indexed")
