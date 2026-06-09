"""Tests for conductor.dag — CONTRACTS_CONSUMED parsing, cycle detection, ordering.

The DAGBuilder resolves a "(Agent N)" reference to whichever agent_id in the prompt
mapping carries the trailing number N, so these tests use the unpadded "agent_1" id
form; the matching real-session test below confirms the zero-padded "agent_001" form
resolves identically.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from conductor.dag import DAGBuilder, DependencyNode

if TYPE_CHECKING:
    from pathlib import Path


def _write_prompt(tmp_path: Path, agent_id: str, body: str) -> Path:
    path = tmp_path / f"{agent_id}.md"
    path.write_text(body)
    return path


def test_no_contracts_consumed_means_no_deps(tmp_path: Path) -> None:
    prompts = {"agent_1": _write_prompt(tmp_path, "agent_1", "Build the allocator. No contract block here.")}
    nodes = DAGBuilder().build(prompts)
    assert nodes["agent_1"].depends_on == []
    assert nodes["agent_1"].required_by == []


def test_single_dependency_parsed_correctly(tmp_path: Path) -> None:
    body = "CONTRACTS_CONSUMED:\n- sentinel-types: ProcessIdentity (Agent 1)\n"
    prompts = {
        "agent_1": _write_prompt(tmp_path, "agent_1", "root, no deps"),
        "agent_2": _write_prompt(tmp_path, "agent_2", body),
    }
    nodes = DAGBuilder().build(prompts)
    assert nodes["agent_2"].depends_on == ["agent_1"]
    assert nodes["agent_1"].required_by == ["agent_2"]


def test_multiple_dependencies_parsed(tmp_path: Path) -> None:
    body = (
        "CONTRACTS_CONSUMED:\n"
        "- sentinel-types: ProcessIdentity (Agent 1)\n"
        "- sentinel-core: SentinelTransport (Agent 3)\n"
    )
    prompts = {
        "agent_1": _write_prompt(tmp_path, "agent_1", "root"),
        "agent_3": _write_prompt(tmp_path, "agent_3", "root"),
        "agent_4": _write_prompt(tmp_path, "agent_4", body),
    }
    nodes = DAGBuilder().build(prompts)
    assert nodes["agent_4"].depends_on == ["agent_1", "agent_3"]
    assert nodes["agent_1"].required_by == ["agent_4"]
    assert nodes["agent_3"].required_by == ["agent_4"]


def test_only_consumed_section_is_scanned(tmp_path: Path) -> None:
    # A "(Agent 2)" mention outside CONTRACTS_CONSUMED must not become a dependency.
    body = (
        "Coordinate with the other agents (Agent 2) as you see fit.\n"
        "CONTRACTS_CONSUMED:\n- sentinel-types: ProcessIdentity (Agent 1)\n"
        "MODIFIES_EXISTING:\n- something (Agent 3)\n"
    )
    prompts = {
        "agent_1": _write_prompt(tmp_path, "agent_1", "root"),
        "agent_2": _write_prompt(tmp_path, "agent_2", "root"),
        "agent_3": _write_prompt(tmp_path, "agent_3", "root"),
        "agent_4": _write_prompt(tmp_path, "agent_4", body),
    }
    nodes = DAGBuilder().build(prompts)
    assert nodes["agent_4"].depends_on == ["agent_1"]


def test_cycle_detection_raises(tmp_path: Path) -> None:
    a1 = "CONTRACTS_CONSUMED:\n- x: T (Agent 2)\n"
    a2 = "CONTRACTS_CONSUMED:\n- y: U (Agent 1)\n"
    prompts = {
        "agent_1": _write_prompt(tmp_path, "agent_1", a1),
        "agent_2": _write_prompt(tmp_path, "agent_2", a2),
    }
    with pytest.raises(ValueError, match="cycle"):
        DAGBuilder().build(prompts)


def test_topological_order_roots_first(tmp_path: Path) -> None:
    body = "CONTRACTS_CONSUMED:\n- x: T (Agent 1)\n"
    prompts = {
        "agent_1": _write_prompt(tmp_path, "agent_1", "root"),
        "agent_2": _write_prompt(tmp_path, "agent_2", body),
    }
    builder = DAGBuilder()
    nodes = builder.build(prompts)
    assert builder.topological_order(nodes) == ["agent_1", "agent_2"]


def test_ready_agents_returns_unblocked_only(tmp_path: Path) -> None:
    body = "CONTRACTS_CONSUMED:\n- x: T (Agent 1)\n"
    prompts = {
        "agent_1": _write_prompt(tmp_path, "agent_1", "root"),
        "agent_2": _write_prompt(tmp_path, "agent_2", body),
    }
    builder = DAGBuilder()
    nodes = builder.build(prompts)

    # agent_1 not done → agent_2 is blocked, only agent_1 is ready.
    ready = builder.ready_agents(nodes, done=set())
    assert "agent_2" not in ready
    assert "agent_1" in ready

    # agent_1 done → agent_2 becomes ready, agent_1 drops out (already done).
    ready = builder.ready_agents(nodes, done={"agent_1"})
    assert ready == ["agent_2"]


def test_missing_dependency_reference_ignored_gracefully(tmp_path: Path) -> None:
    body = "CONTRACTS_CONSUMED:\n- ghost: T (Agent 99)\n"
    prompts = {"agent_1": _write_prompt(tmp_path, "agent_1", body)}
    nodes = DAGBuilder().build(prompts)  # must not raise
    assert nodes["agent_1"].depends_on == []


def test_no_deps_session_behaves_identically(tmp_path: Path) -> None:
    # Backward compatibility: every agent lacks a CONTRACTS_CONSUMED block, so all are
    # roots and all are ready from the first call — today's "launch everyone" behaviour.
    prompts = {
        f"agent_{i}": _write_prompt(tmp_path, f"agent_{i}", f"do work {i}")
        for i in range(1, 5)
    }
    builder = DAGBuilder()
    nodes = builder.build(prompts)
    assert all(node.depends_on == [] for node in nodes.values())
    assert set(builder.ready_agents(nodes, done=set())) == set(prompts)


def test_zero_padded_agent_ids_resolve(tmp_path: Path) -> None:
    # Real sessions use zero-padded ids ("agent_001"); a "(Agent 1)" reference must
    # still resolve to "agent_001".
    body = "CONTRACTS_CONSUMED:\n- sentinel-types: ProcessIdentity (Agent 1)\n"
    prompts = {
        "agent_001": _write_prompt(tmp_path, "agent_001", "root"),
        "agent_002": _write_prompt(tmp_path, "agent_002", body),
    }
    nodes = DAGBuilder().build(prompts)
    assert nodes["agent_002"].depends_on == ["agent_001"]
    assert nodes["agent_001"].required_by == ["agent_002"]


def test_empty_session_builds_empty_graph() -> None:
    assert DAGBuilder().build({}) == {}


def test_dependency_node_is_json_serialisable() -> None:
    # Round-trips through json — only strings/lists, as the session manifest requires.
    import json

    node = DependencyNode(agent_id="agent_1", depends_on=["agent_0"], required_by=["agent_2"])
    payload = {
        "agent_id": node.agent_id,
        "depends_on": node.depends_on,
        "required_by": node.required_by,
    }
    assert json.loads(json.dumps(payload)) == payload
