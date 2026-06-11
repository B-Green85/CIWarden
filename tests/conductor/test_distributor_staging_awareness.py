"""Tests for final-stage agent detection and STAGING AWARENESS injection.

A final-stage agent is the DAG sink — its dependencies cover every other agent in the
session. Sentinel v3's Agent 8 was exactly that: it depended on all seven others, launched,
found no v3 work in the repo (its dependencies were still in staging, uncommitted), and
stood down. distribute() now appends a STAGING AWARENESS block to such agents pointing them
at the sibling staging directories.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from conductor import distributor
from conductor.dag import DAGBuilder
from conductor.distributor import _covers_all_others
from conductor.session import AgentSpec, ConductorSession

if TYPE_CHECKING:
    from pathlib import Path

_MARKER = "STAGING AWARENESS"


def _consumes(*agent_nums: int) -> str:
    """A CONTRACTS_CONSUMED block declaring a dependency on each given agent number."""
    lines = "\n".join(f"- dep: Thing (Agent {n})" for n in agent_nums)
    return f"CONTRACTS_CONSUMED:\n{lines}\nMODIFIES_EXISTING: no\n"


def _session(tmp_path: Path, specs: list[tuple[int, str]]) -> ConductorSession:
    agents = [
        AgentSpec(f"agent_{n:03d}", f"agent {n}", body, f"agent_{n}")
        for n, body in specs
    ]
    return ConductorSession(
        repo_root=tmp_path / "repo",
        repo_name="demo",
        agents=agents,
        staging_base=tmp_path / "base",
        staging_root=tmp_path / "staging",
    )


def _dir(session: ConductorSession, agent_id: str) -> str:
    agent = session.agent_by_id(agent_id)
    assert agent is not None
    return str(distributor.staging_dir(session, agent))


def _prompt(session: ConductorSession, agent_id: str) -> str:
    agent = session.agent_by_id(agent_id)
    assert agent is not None
    return (distributor.staging_dir(session, agent) / distributor.PROMPT_FILE).read_text()


def _awareness_block(session: ConductorSession, agent_id: str) -> str:
    """The injected block only (everything after the marker), so COMMIT PROTOCOL paths that
    legitimately mention the agent's own dir don't bleed into block assertions."""
    return _prompt(session, agent_id).split(_MARKER, 1)[1]


# ── Detection predicate ───────────────────────────────────────────


def test_covers_all_others_predicate() -> None:
    all_ids = {"agent_001", "agent_002", "agent_003"}
    assert _covers_all_others("agent_003", ["agent_001", "agent_002"], all_ids) is True
    # order-independent
    assert _covers_all_others("agent_003", ["agent_002", "agent_001"], all_ids) is True
    # misses one peer → not final-stage
    assert _covers_all_others("agent_003", ["agent_001"], all_ids) is False
    # no dependencies → never final-stage (nothing to be aware of)
    assert _covers_all_others("agent_001", [], all_ids) is False


# ── Detection over a session ──────────────────────────────────────


def test_final_stage_agent_detected(tmp_path: Path) -> None:
    session = _session(tmp_path, [(1, "root"), (2, _consumes(1)), (3, _consumes(1, 2))])
    distributor.distribute(session)
    assert distributor.final_stage_agents(session) == ["agent_003"]


def test_two_agent_chain_consumer_is_final_stage(tmp_path: Path) -> None:
    session = _session(tmp_path, [(1, "root"), (2, _consumes(1))])
    distributor.distribute(session)
    assert distributor.final_stage_agents(session) == ["agent_002"]


def test_single_agent_session_has_no_final_stage(tmp_path: Path) -> None:
    session = _session(tmp_path, [(1, "lone agent, no deps")])
    distributor.distribute(session)
    assert distributor.final_stage_agents(session) == []


def test_independent_agents_have_no_final_stage(tmp_path: Path) -> None:
    session = _session(tmp_path, [(1, "a"), (2, "b"), (3, "c")])  # no CONTRACTS_CONSUMED
    distributor.distribute(session)
    assert distributor.final_stage_agents(session) == []


def test_partial_dependency_is_not_final_stage(tmp_path: Path) -> None:
    # agent_3 depends only on agent_1, not agent_2 → does not cover all others.
    session = _session(tmp_path, [(1, "root"), (2, "root"), (3, _consumes(1))])
    distributor.distribute(session)
    assert distributor.final_stage_agents(session) == []


# ── Injection ─────────────────────────────────────────────────────


def test_final_stage_agent_gets_staging_awareness_block(tmp_path: Path) -> None:
    session = _session(tmp_path, [(1, "root"), (2, _consumes(1)), (3, _consumes(1, 2))])
    distributor.distribute(session)
    assert _MARKER in _prompt(session, "agent_003")


def test_non_final_stage_agents_are_not_injected(tmp_path: Path) -> None:
    session = _session(tmp_path, [(1, "root"), (2, _consumes(1)), (3, _consumes(1, 2))])
    distributor.distribute(session)
    assert _MARKER not in _prompt(session, "agent_001")
    assert _MARKER not in _prompt(session, "agent_002")


def test_block_lists_each_dependency_staging_dir(tmp_path: Path) -> None:
    session = _session(tmp_path, [(1, "root"), (2, _consumes(1)), (3, _consumes(1, 2))])
    distributor.distribute(session)
    block = _awareness_block(session, "agent_003")
    assert f"{_dir(session, 'agent_001')}/" in block
    assert f"{_dir(session, 'agent_002')}/" in block
    # the agent's own staging dir is never listed among its dependencies
    assert f"{_dir(session, 'agent_003')}/" not in block


def test_no_injection_for_single_or_independent_sessions(tmp_path: Path) -> None:
    lone = _session(tmp_path / "a", [(1, "lone")])
    distributor.distribute(lone)
    assert _MARKER not in _prompt(lone, "agent_001")

    indep = _session(tmp_path / "b", [(1, "a"), (2, "b")])
    distributor.distribute(indep)
    assert _MARKER not in _prompt(indep, "agent_001")
    assert _MARKER not in _prompt(indep, "agent_002")


def test_injection_does_not_change_dependency_parsing(tmp_path: Path) -> None:
    # The appended block must not perturb the graph cli.build_dag parses afterwards: its
    # listed "agent_001/" dirs are not "(Agent N)" references.
    session = _session(tmp_path, [(1, "root"), (2, _consumes(1)), (3, _consumes(1, 2))])
    distributor.distribute(session)  # injects into agent_003
    prompt_map = {
        a.agent_id: distributor.staging_dir(session, a) / distributor.PROMPT_FILE
        for a in session.agents
    }
    nodes = DAGBuilder().build(prompt_map)  # mirrors cli.build_dag on the injected prompts
    assert nodes["agent_003"].depends_on == ["agent_001", "agent_002"]
    assert nodes["agent_002"].depends_on == ["agent_001"]
    assert nodes["agent_001"].depends_on == []


def test_sentinel_style_final_stage_via_range_notation(tmp_path: Path) -> None:
    # Agent 8's real notation — "All agents 1–7" — must resolve to all seven peers and make
    # agent_008 the final-stage agent, with every dependency's staging dir listed.
    specs: list[tuple[int, str]] = [(n, "root") for n in range(1, 8)]
    specs.append((
        8,
        "CONTRACTS_CONSUMED:\n- All agents 1–7: every type and interface they produced\n"
        "MODIFIES_EXISTING: yes\n",
    ))
    session = _session(tmp_path, specs)
    distributor.distribute(session)
    assert distributor.final_stage_agents(session) == ["agent_008"]
    block = _awareness_block(session, "agent_008")
    for n in range(1, 8):
        assert f"{_dir(session, f'agent_{n:03d}')}/" in block
