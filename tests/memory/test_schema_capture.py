"""Tests for memory.schema_capture — static, LLM-free contract extraction."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from memory.schema_capture import SchemaCaptureClient, _flatten_exposes, _get_repo_name

if TYPE_CHECKING:
    from pathlib import Path

PY_SOURCE = """
import os
from pathlib import Path

class Foo:
    def bar(self): ...
    def _hidden(self): ...

def top_level(): ...
def _private(): ...
"""

RUST_SOURCE = """
use core::mem;
use alloc::vec::Vec;
// requires: memory initialized before call
// assumes no_std
pub fn init() -> Result<(), KernelError> {}
pub struct Scheduler;
pub trait Run {}
pub enum State { A, B }
"""


def _schema(modules: dict[str, Any], module_key: str | None = None) -> dict[str, Any]:
    out = {"repo": "X", "session_id": "s", "modules": modules}
    if module_key:
        out["module_key"] = module_key
    return out


@pytest.fixture()
def client(tmp_path: Path) -> SchemaCaptureClient:
    return SchemaCaptureClient(schema_path=tmp_path / "absent.json", repo_name="X")


class TestSchemaPath:
    @pytest.mark.asyncio
    async def test_schema_present_produces_summary(self, tmp_path: Path) -> None:
        sp = tmp_path / "schema.json"
        sp.write_text(json.dumps(_schema({"scheduler": {
            "contracts": [{"type": "function", "name": "init", "consumed_by": ["kmain"]}],
            "exposes": [{"symbol": "scheduler::init", "signature": "pub fn init()"}],
            "assumptions": ["no_std"], "dependencies": ["core"],
        }}, module_key="scheduler")))
        c = SchemaCaptureClient(schema_path=sp, repo_name="X")
        s = await c.extract_contracts("", "scheduler", "gen_1", 1)
        assert s.module == "scheduler"
        assert s.contracts[0].name == "init"
        assert s.contracts[0].consumed_by == ["kmain"]
        assert s.exposes == ["scheduler::init"]
        assert s.assumptions == ["no_std"]
        assert s.dependencies == ["core"]

    @pytest.mark.asyncio
    async def test_schema_missing_falls_back_to_ast(self, client: SchemaCaptureClient) -> None:
        s = await client.extract_contracts("def x(): ...", "m", "g", 1)
        assert [c.name for c in s.contracts] == ["x"]

    @pytest.mark.asyncio
    async def test_malformed_schema_falls_back_to_ast(self, tmp_path: Path) -> None:
        sp = tmp_path / "schema.json"
        sp.write_text("{ not valid json")
        c = SchemaCaptureClient(schema_path=sp, repo_name="X")
        s = await c.extract_contracts("def y(): ...", "m", "g", 1)
        assert [c.name for c in s.contracts] == ["y"]

    @pytest.mark.asyncio
    async def test_schema_missing_module_falls_back_to_ast(self, tmp_path: Path) -> None:
        sp = tmp_path / "schema.json"
        sp.write_text(json.dumps(_schema({"other": {"exposes": []}})))
        c = SchemaCaptureClient(schema_path=sp, repo_name="X")
        s = await c.extract_contracts("def z(): ...", "scheduler", "g", 1)
        assert [c.name for c in s.contracts] == ["z"]

    @pytest.mark.asyncio
    async def test_module_key_overrides_module_name(self, tmp_path: Path) -> None:
        sp = tmp_path / "schema.json"
        sp.write_text(json.dumps(_schema(
            {"sched": {"contracts": [{"type": "function", "name": "go"}], "exposes": []}},
            module_key="sched",
        )))
        c = SchemaCaptureClient(schema_path=sp, repo_name="X")
        # discovered module name "scheduler" != schema key "sched"; module_key resolves it
        s = await c.extract_contracts("", "scheduler", "g", 1)
        assert s.contracts[0].name == "go"


class TestAstPython:
    @pytest.mark.asyncio
    async def test_classes_functions_imports(self, client: SchemaCaptureClient) -> None:
        s = await client.extract_contracts(PY_SOURCE, "mymod", "g", 1)
        names = {(c.type.value, c.name) for c in s.contracts}
        assert ("class", "Foo") in names
        assert ("function", "top_level") in names
        foo = next(c for c in s.contracts if c.name == "Foo")
        assert "bar" in foo.methods
        assert set(s.dependencies) == {"os", "pathlib"}
        assert "mymod::Foo" in s.exposes
        assert "mymod::_private" not in s.exposes  # private not exposed


class TestAstRust:
    @pytest.mark.asyncio
    async def test_rust_regex_extraction(self, client: SchemaCaptureClient) -> None:
        s = await client.extract_contracts(RUST_SOURCE, "scheduler", "g", 1)
        kinds = {(c.type.value, c.name) for c in s.contracts}
        assert ("function", "init") in kinds
        assert ("class", "Scheduler") in kinds
        assert ("interface", "Run") in kinds
        assert ("enum", "State") in kinds
        assert set(s.dependencies) == {"core", "alloc"}

    @pytest.mark.asyncio
    async def test_rust_assumption_comments(self, client: SchemaCaptureClient) -> None:
        s = await client.extract_contracts(RUST_SOURCE, "scheduler", "g", 1)
        assert any("memory initialized" in a for a in s.assumptions)
        assert any("no_std" in a for a in s.assumptions)


class TestRepoName:
    def test_git_remote_present(self) -> None:
        # This repo has a git remote; name resolves non-empty with no spaces.
        name = _get_repo_name()
        assert name and " " not in name

    def test_directory_fallback(self, tmp_path: Path) -> None:
        # No git remote in a bare temp dir → directory name (hyphens preserved).
        hyphen_dir = tmp_path / "my-cool-repo"
        hyphen_dir.mkdir()
        assert _get_repo_name(hyphen_dir) == "my-cool-repo"


class TestFlattenExposes:
    def test_objects_and_strings(self) -> None:
        assert _flatten_exposes([{"symbol": "a::b"}, "c::d"]) == ["a::b", "c::d"]

    def test_non_list(self) -> None:
        assert _flatten_exposes(None) == []
