"""Static, LLM-free contract extraction for the Generational Memory Gate.

`SchemaCaptureClient` replaces `AnthropicClient` as the extraction layer. Two
strategies, tried in order:

1. Agent-declared schema — read ``.cdmad/session_schema.json`` directly. Primary
   path for all Conductor-managed sessions.
2. AST/static fallback — Python via the ``ast`` module, Rust via regex. Fires when
   no schema file is present (cold start, or an agent run outside the Conductor).

No LLM API call. No network. No latency. This is the canonical static-analysis
implementation — the conductor package imports from here, never the reverse.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path

from memory.llm_client import LLMClient
from memory.models import ContractEntry, ContractSummary, ContractType

# ── Repo name resolution ─────────────────────────────────────────
# Canonical home for _get_repo_name. Defined in the memory layer (the lower
# layer) so conductor.vdb_io can import it from here without a circular import.


def _get_repo_name(repo_root: str | Path = ".") -> str:
    """Resolve a repo name: git remote 'origin' basename, else directory name.

    Hyphens in names are preserved verbatim (e.g. ``ci-wrapper``).
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
        )
        url = result.stdout.strip()
        if result.returncode == 0 and url:
            basename = url.rstrip("/").split("/")[-1]
            return basename.removesuffix(".git")
    except OSError:
        pass
    return Path(repo_root).resolve().name


# ── Rust static-analysis patterns ────────────────────────────────

_RUST_FN = re.compile(r"^\s*pub(?:\s*\([^)]*\))?\s+(?:async\s+|unsafe\s+|const\s+)*fn\s+(\w+)", re.MULTILINE)
_RUST_STRUCT = re.compile(r"^\s*pub(?:\s*\([^)]*\))?\s+struct\s+(\w+)", re.MULTILINE)
_RUST_TRAIT = re.compile(r"^\s*pub(?:\s*\([^)]*\))?\s+trait\s+(\w+)", re.MULTILINE)
_RUST_ENUM = re.compile(r"^\s*pub(?:\s*\([^)]*\))?\s+enum\s+(\w+)", re.MULTILINE)
_RUST_USE = re.compile(r"^\s*use\s+([A-Za-z_][\w:]*)", re.MULTILINE)
_RUST_ASSUMPTION = re.compile(r"//\s*(?:must|requires|assumes)\b[:\s]*(.+)$", re.IGNORECASE | re.MULTILINE)


class SchemaCaptureClient(LLMClient):
    """Reads contracts from an agent-declared schema, or falls back to AST.

    No LLM API call. No network. No latency.
    """

    def __init__(
        self,
        schema_path: str | Path | None = None,
        repo_name: str | None = None,
    ) -> None:
        self.schema_path = Path(schema_path or ".cdmad/session_schema.json")
        self.repo_name = repo_name or _get_repo_name()

    async def extract_contracts(
        self,
        source_code: str,
        module_name: str,
        generation_id: str,
        sequence: int,
    ) -> ContractSummary:
        """Schema-first extraction, AST fallback on absence/malformed/missing-module."""
        if self.schema_path.exists():
            summary = self._from_schema(module_name, generation_id, sequence)
            if summary is not None:
                return summary
        return self._from_ast(source_code, module_name, generation_id, sequence)

    # ── Strategy 1: agent-declared schema ────────────────────────

    def _from_schema(
        self,
        module_name: str,
        generation_id: str,
        sequence: int,
    ) -> ContractSummary | None:
        """Read from agent-declared session_schema.json.

        Resolves the module entry by ``module_key`` when present (Conductor-stamped),
        falling back to ``module_name``. Returns None on malformed JSON or a missing
        module entry so the caller can fall back to AST.
        """
        try:
            data = json.loads(self.schema_path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

        modules = data.get("modules")
        if not isinstance(modules, dict):
            return None

        key = data.get("module_key") or module_name
        entry = modules.get(key)
        if entry is None and key != module_name:
            entry = modules.get(module_name)
        if not isinstance(entry, dict):
            return None

        contracts = [
            ContractEntry(
                type=ContractType(c.get("type", "function")),
                name=c.get("name", ""),
                fields=list(c.get("fields", [])),
                consumed_by=list(c.get("consumed_by", [])),
                methods=list(c.get("methods", [])),
            )
            for c in entry.get("contracts", [])
            if isinstance(c, dict)
        ]

        return ContractSummary(
            module=module_name,
            generation_id=generation_id,
            sequence=sequence,
            contracts=contracts,
            assumptions=list(entry.get("assumptions", [])),
            dependencies=list(entry.get("dependencies", [])),
            exposes=_flatten_exposes(entry.get("exposes", [])),
        )

    # ── Strategy 2: AST / static fallback ────────────────────────

    def _from_ast(
        self,
        source_code: str,
        module_name: str,
        generation_id: str,
        sequence: int,
    ) -> ContractSummary:
        """Static analysis fallback. Python via ast; Rust via regex on parse failure."""
        try:
            tree = ast.parse(source_code)
        except SyntaxError:
            return self._from_rust(source_code, module_name, generation_id, sequence)
        return self._from_python(tree, module_name, generation_id, sequence)

    def _from_python(
        self,
        tree: ast.Module,
        module_name: str,
        generation_id: str,
        sequence: int,
    ) -> ContractSummary:
        contracts: list[ContractEntry] = []
        dependencies: set[str] = set()
        exposes: list[str] = []

        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                methods = [
                    n.name
                    for n in node.body
                    if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
                ]
                contracts.append(
                    ContractEntry(type=ContractType.CLASS, name=node.name, methods=methods)
                )
                if not node.name.startswith("_"):
                    exposes.append(f"{module_name}::{node.name}")
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                contracts.append(ContractEntry(type=ContractType.FUNCTION, name=node.name))
                if not node.name.startswith("_"):
                    exposes.append(f"{module_name}::{node.name}")

        for walked in ast.walk(tree):
            if isinstance(walked, ast.Import):
                for alias in walked.names:
                    dependencies.add(alias.name.split(".")[0])
            elif isinstance(walked, ast.ImportFrom) and walked.module:
                dependencies.add(walked.module.split(".")[0])

        return ContractSummary(
            module=module_name,
            generation_id=generation_id,
            sequence=sequence,
            contracts=contracts,
            assumptions=[],
            dependencies=sorted(dependencies),
            exposes=exposes,
        )

    def _from_rust(
        self,
        source_code: str,
        module_name: str,
        generation_id: str,
        sequence: int,
    ) -> ContractSummary:
        contracts: list[ContractEntry] = []
        exposes: list[str] = []

        def _add(pattern: re.Pattern[str], ctype: ContractType) -> None:
            for name in pattern.findall(source_code):
                contracts.append(ContractEntry(type=ctype, name=name))
                exposes.append(f"{module_name}::{name}")

        _add(_RUST_FN, ContractType.FUNCTION)
        _add(_RUST_STRUCT, ContractType.CLASS)
        _add(_RUST_TRAIT, ContractType.INTERFACE)
        _add(_RUST_ENUM, ContractType.ENUM)

        dependencies = sorted({u.split("::")[0] for u in _RUST_USE.findall(source_code)})
        assumptions = [a.strip() for a in _RUST_ASSUMPTION.findall(source_code) if a.strip()]

        return ContractSummary(
            module=module_name,
            generation_id=generation_id,
            sequence=sequence,
            contracts=contracts,
            assumptions=assumptions,
            dependencies=dependencies,
            exposes=exposes,
        )


def _flatten_exposes(raw: object) -> list[str]:
    """Flatten schema 'exposes' to list[str].

    Accepts rich objects ({"symbol": ...}) from Conductor schemas or plain strings
    from the solo path. The ContractSummary model carries only symbol names — the
    full signature/assumption detail stays in the raw schema for the PeerChecker.
    """
    if not isinstance(raw, list):
        return []
    result: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            symbol = item.get("symbol")
            if symbol:
                result.append(str(symbol))
        elif isinstance(item, str):
            result.append(item)
    return result
