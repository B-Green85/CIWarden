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


# ── Regression: real CONTRACTS_CONSUMED notations from the Sentinel v3 session ──
# Before the fix, AGENT_REF_PATTERN only matched a number *inside* parens — "(Agent N)" —
# so every other real notation parsed as "no dependencies" (DEVLOG: agents 2,3,4,7,8 all
# showed no deps; only 5 and 6, which used "(Agent 1)", resolved). Each test below uses a
# notation taken verbatim from the prompt that exposed the gap.


def test_bare_agent_reference_with_colon(tmp_path: Path) -> None:
    # Agent 4's real block: "- Agent 3: SentinelTransport (...)" — number not parenthesised.
    body = (
        "CONTRACTS_CONSUMED:\n"
        "- sentinel-types: ProcessIdentity, InterceptionEvent\n"
        "- Agent 3: SentinelTransport (transport layer, for socket auth integration)\n"
        "MODIFIES_EXISTING: yes\n"
    )
    prompts = {
        "agent_003": _write_prompt(tmp_path, "agent_003", "root"),
        "agent_004": _write_prompt(tmp_path, "agent_004", body),
    }
    nodes = DAGBuilder().build(prompts)
    assert nodes["agent_004"].depends_on == ["agent_003"]


def test_number_before_parenthetical_label(tmp_path: Path) -> None:
    # Agent 7's real block: the number precedes a *label* in parens — "Agent 1 (sentinel-types)".
    body = (
        "CONTRACTS_CONSUMED:\n"
        "- Agent 1 (sentinel-types): SentinelCapability trait, AgentId, DegradationEvent\n"
        "- Agent 3 (transport): KernelTransport stub\n"
        "- Agent 5 (controls): SentinelCapability trait interface\n"
        "- Agent 6 (audit): ChainedAuditEntry format\n"
        "- GolemLinux src/syscall/: wire intercept() into dispatch path\n"
        "MODIFIES_EXISTING:\n"
    )
    prompts = {
        "agent_001": _write_prompt(tmp_path, "agent_001", "root"),
        "agent_003": _write_prompt(tmp_path, "agent_003", "root"),
        "agent_005": _write_prompt(tmp_path, "agent_005", "root"),
        "agent_006": _write_prompt(tmp_path, "agent_006", "root"),
        "agent_007": _write_prompt(tmp_path, "agent_007", body),
    }
    nodes = DAGBuilder().build(prompts)
    assert nodes["agent_007"].depends_on == ["agent_001", "agent_003", "agent_005", "agent_006"]


def test_plural_agents_inclusive_range(tmp_path: Path) -> None:
    # Agent 8's real block: "All agents 1–7" — plural keyword, en-dash range, no parens.
    body = (
        "CONTRACTS_CONSUMED:\n"
        "- All agents 1–7: every type and interface they produced\n"
        "- sentinel-py: existing bindings (unchanged)\n"
        "MODIFIES_EXISTING: yes\n"
    )
    prompts = {
        f"agent_{n:03d}": _write_prompt(tmp_path, f"agent_{n:03d}", "root")
        for n in range(1, 8)
    }
    prompts["agent_008"] = _write_prompt(tmp_path, "agent_008", body)
    nodes = DAGBuilder().build(prompts)
    assert nodes["agent_008"].depends_on == [f"agent_{n:03d}" for n in range(1, 8)]


def test_agent_identifier_tokens_do_not_match(tmp_path: Path) -> None:
    # "AgentId" / "AgentEvent" are type names, not references — they must NOT create deps,
    # else every agent consuming those types would gain a phantom dependency.
    body = (
        "CONTRACTS_CONSUMED:\n"
        "- sentinel-types: AgentId, DegradationEvent, AgentEvent (no real dependency)\n"
        "MODIFIES_EXISTING: no\n"
    )
    prompts = {
        "agent_001": _write_prompt(tmp_path, "agent_001", "root"),
        "agent_002": _write_prompt(tmp_path, "agent_002", body),
    }
    nodes = DAGBuilder().build(prompts)
    assert nodes["agent_002"].depends_on == []


def test_comma_and_word_list(tmp_path: Path) -> None:
    # A list ("1, 2 and 4") names distinct agents — NOT a range.
    body = "CONTRACTS_CONSUMED:\n- Agents 1, 2 and 4: shared types\nMODIFIES_EXISTING:\n"
    prompts = {
        f"agent_{n:03d}": _write_prompt(tmp_path, f"agent_{n:03d}", "root")
        for n in (1, 2, 4)
    }
    prompts["agent_005"] = _write_prompt(tmp_path, "agent_005", body)
    nodes = DAGBuilder().build(prompts)
    assert nodes["agent_005"].depends_on == ["agent_001", "agent_002", "agent_004"]


def test_range_with_to_keyword(tmp_path: Path) -> None:
    # A worded range ("1 to 3") expands inclusively, like the en-dash form.
    body = "CONTRACTS_CONSUMED:\n- agents 1 to 3: foundation types\nMODIFIES_EXISTING:\n"
    prompts = {
        f"agent_{n:03d}": _write_prompt(tmp_path, f"agent_{n:03d}", "root")
        for n in (1, 2, 3)
    }
    prompts["agent_004"] = _write_prompt(tmp_path, "agent_004", body)
    nodes = DAGBuilder().build(prompts)
    assert nodes["agent_004"].depends_on == ["agent_001", "agent_002", "agent_003"]


def test_crate_name_only_reference_is_unresolved(tmp_path: Path) -> None:
    # Known limitation, documented: Agents 2 & 3 referenced producers ONLY by crate name
    # ("sentinel-types") with no "Agent N" token, so a number-based parser cannot resolve
    # them. Such prompts must name the agent explicitly (as Agents 5/6 did via "(Agent 1)").
    body = "CONTRACTS_CONSUMED:\n- sentinel-types (ProcessIdentity)\nMODIFIES_EXISTING: false\n"
    prompts = {
        "agent_001": _write_prompt(tmp_path, "agent_001", "root"),
        "agent_002": _write_prompt(tmp_path, "agent_002", body),
    }
    nodes = DAGBuilder().build(prompts)
    assert nodes["agent_002"].depends_on == []


def test_sentinel_v3_full_session_graph(tmp_path: Path) -> None:
    # End-to-end regression over all eight Sentinel v3 blocks (notations verbatim). This is
    # the exact session that exposed the bug. After the fix, agents 4, 7, 8 resolve (they
    # did not before); only the crate-name-only agents (2, 3) remain rootless — a
    # prompt-authoring gap, not a parser one.
    blocks = {
        1: "CONTRACTS_CONSUMED: none\nMODIFIES_EXISTING: false\n",
        2: "CONTRACTS_CONSUMED:\n"
           "- sentinel-types (ProcessIdentity — for binary_hash field format)\n"
           "MODIFIES_EXISTING: false\n",
        3: "CONTRACTS_CONSUMED:\n"
           "- sentinel-types: ProcessIdentity\n"
           "- sentinel-core: SentinelConfig (existing), SentinelError (existing)\n"
           "MODIFIES_EXISTING: yes\n",
        4: "CONTRACTS_CONSUMED:\n"
           "- sentinel-types: ProcessIdentity, InterceptionEvent\n"
           "- Agent 3: SentinelTransport (transport layer, for socket auth integration)\n"
           "MODIFIES_EXISTING: yes\n",
        5: "CONTRACTS_CONSUMED:\n"
           "- sentinel-types: AgentId, DegradationEvent, ProcessIdentity (Agent 1)\n"
           "- sentinel-core: SentinelError, audit write channel\n"
           "MODIFIES_EXISTING: yes\n",
        6: "CONTRACTS_CONSUMED:\n"
           "- sentinel-types: ChainedAuditEntry, AgentId, AuditEvent (Agent 1)\n"
           "- GolemLinux src/sentinel/: SHA-256 implementation (copy, do not import)\n"
           "MODIFIES_EXISTING: yes\n",
        7: "CONTRACTS_CONSUMED:\n"
           "- Agent 1 (sentinel-types): SentinelCapability trait, AgentId\n"
           "- Agent 3 (transport): KernelTransport stub\n"
           "- Agent 5 (controls): trait interface\n"
           "- Agent 6 (audit): ChainedAuditEntry format\n"
           "MODIFIES_EXISTING:\n",
        8: "CONTRACTS_CONSUMED:\n"
           "- All agents 1–7: every type and interface they produced\n"
           "- sentinel-py: existing bindings (unchanged)\n"
           "MODIFIES_EXISTING: yes\n",
    }
    prompts = {
        f"agent_{n:03d}": _write_prompt(tmp_path, f"agent_{n:03d}", blocks[n])
        for n in range(1, 9)
    }
    nodes = DAGBuilder().build(prompts)
    got = {aid: nodes[aid].depends_on for aid in sorted(nodes)}
    assert got == {
        "agent_001": [],
        "agent_002": [],  # crate-name-only ref — known unresolved
        "agent_003": [],  # crate-name-only ref — known unresolved
        "agent_004": ["agent_003"],
        "agent_005": ["agent_001"],
        "agent_006": ["agent_001"],
        "agent_007": ["agent_001", "agent_003", "agent_005", "agent_006"],
        "agent_008": [
            "agent_001", "agent_002", "agent_003", "agent_004",
            "agent_005", "agent_006", "agent_007",
        ],
    }


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
