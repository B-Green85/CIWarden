"""Tests for the dependency-aware launch loop, state persistence, and the DAG gate.

Covers conductor.distributor's DAG-session additions: run_dependency_aware (release
ordering + resume seeding), update_state (manifest persistence), and has_dependencies
(the backward-compatibility selector).
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from conductor import distributor
from conductor.dag import DAGBuilder
from conductor.session import SESSION_MANIFEST, AgentSpec, ConductorSession

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_CONSUMER_PROMPT = """\
# Consumer agent

CONTRACTS_CONSUMED:
- core-types: Thing (Agent 1)

CONTRACTS_PRODUCED:
- the consumer
"""

_ROOT_PROMPT = "# Root agent\n\nNo dependencies here.\n"


def _agent(num: int, prompt: str, *, repo: str = "default") -> AgentSpec:
    aid = f"agent_{num:03d}"
    return AgentSpec(
        agent_id=aid,
        description=f"agent {num}",
        prompt=prompt,
        module_key=f"agent_{num}",
        repo=repo,
    )


def _session(tmp_path: Path, agents: list[AgentSpec]) -> ConductorSession:
    return ConductorSession(
        repo_root=tmp_path / "repo",
        repo_name="demo",
        agents=agents,
        staging_base=tmp_path / "base",
        staging_root=tmp_path / "staging",
    )


def _build_dag(session: ConductorSession) -> None:
    """Distribute prompts to disk, then build the DAG over them (mirrors cli.build_dag)."""
    distributor.distribute(session)
    prompt_map = {
        a.agent_id: distributor.staging_dir(session, a) / distributor.PROMPT_FILE
        for a in session.agents
    }
    session.dag = DAGBuilder().build(prompt_map)
    for a in session.agents:
        node = session.dag.get(a.agent_id)
        if node is not None:
            a.depends_on = node.depends_on


class TestHasDependencies:
    def test_false_when_no_edges(self, tmp_path: Path) -> None:
        session = _session(tmp_path, [_agent(1, _ROOT_PROMPT), _agent(2, _ROOT_PROMPT)])
        _build_dag(session)
        assert distributor.has_dependencies(session) is False

    def test_true_when_any_edge(self, tmp_path: Path) -> None:
        session = _session(tmp_path, [_agent(1, _ROOT_PROMPT), _agent(2, _CONSUMER_PROMPT)])
        _build_dag(session)
        assert distributor.has_dependencies(session) is True
        assert session.agents[1].depends_on == ["agent_001"]


class TestUpdateState:
    def test_persists_to_manifest(self, tmp_path: Path) -> None:
        session = _session(tmp_path, [_agent(1, _ROOT_PROMPT)])
        distributor.update_state(session, "agent_001", "running")
        data = json.loads((session.staging_base / SESSION_MANIFEST).read_text())
        states = {a["agent_id"]: a["state"] for a in data["agents"]}
        assert states["agent_001"] == "running"

    def test_invalid_state_raises(self, tmp_path: Path) -> None:
        session = _session(tmp_path, [_agent(1, _ROOT_PROMPT)])
        with pytest.raises(ValueError, match="invalid agent state"):
            distributor.update_state(session, "agent_001", "nonsense")

    def test_unknown_agent_is_noop(self, tmp_path: Path) -> None:
        session = _session(tmp_path, [_agent(1, _ROOT_PROMPT)])
        distributor.update_state(session, "agent_999", "running")  # must not raise


def _autocomplete_launcher(
    record: list[tuple[str, set[str]]],
) -> Callable[[ConductorSession, AgentSpec], None]:
    """A launch_fn that records (agent_id, deps-already-done) then signals .done.

    Recording the done-set *before* writing this agent's own .done lets a test assert
    that a dependent was only launched once its dependency had already signalled.
    """
    def launch(session: ConductorSession, agent: AgentSpec) -> None:
        done_now = {
            a.agent_id
            for a in session.agents
            if distributor.is_done(session, a)
        }
        record.append((agent.agent_id, done_now))
        (distributor.staging_dir(session, agent) / distributor.DONE_FLAG).write_text("")
    return launch


class TestDependencyAwareLaunch:
    def test_dependent_launches_only_after_dependency_done(self, tmp_path: Path) -> None:
        session = _session(tmp_path, [_agent(1, _ROOT_PROMPT), _agent(2, _CONSUMER_PROMPT)])
        _build_dag(session)
        for a in session.agents:
            distributor.clear_done(session, a)

        record: list[tuple[str, set[str]]] = []
        ok = distributor.run_dependency_aware(
            session, launch_fn=_autocomplete_launcher(record), poll_interval=0.01,
        )

        assert ok is True
        launch_order = [aid for aid, _ in record]
        assert launch_order == ["agent_001", "agent_002"]
        # agent_002 must not have been launched until agent_001 had signalled .done.
        deps_at_agent2_launch = dict(record)["agent_002"]
        assert "agent_001" in deps_at_agent2_launch
        # agent_001 (the root) launched with nothing yet done.
        assert dict(record)["agent_001"] == set()

    def test_independent_agents_both_launch(self, tmp_path: Path) -> None:
        session = _session(tmp_path, [_agent(1, _ROOT_PROMPT), _agent(2, _ROOT_PROMPT)])
        _build_dag(session)
        for a in session.agents:
            distributor.clear_done(session, a)

        record: list[tuple[str, set[str]]] = []
        ok = distributor.run_dependency_aware(
            session, launch_fn=_autocomplete_launcher(record), poll_interval=0.01,
        )

        assert ok is True
        assert {aid for aid, _ in record} == {"agent_001", "agent_002"}

    def test_final_states_are_done(self, tmp_path: Path) -> None:
        session = _session(tmp_path, [_agent(1, _ROOT_PROMPT), _agent(2, _CONSUMER_PROMPT)])
        _build_dag(session)
        for a in session.agents:
            distributor.clear_done(session, a)

        record: list[tuple[str, set[str]]] = []
        distributor.run_dependency_aware(
            session, launch_fn=_autocomplete_launcher(record), poll_interval=0.01,
        )
        assert all(a.state == "done" for a in session.agents)

    def test_resume_skips_already_done_agents(self, tmp_path: Path) -> None:
        session = _session(tmp_path, [_agent(1, _ROOT_PROMPT), _agent(2, _CONSUMER_PROMPT)])
        _build_dag(session)
        # Simulate a prior run: agent_001 finished (state done + .done on disk), so a
        # resume must NOT relaunch it — only agent_002 should be launched.
        session.agents[0].state = "done"
        (distributor.staging_dir(session, session.agents[0]) / distributor.DONE_FLAG).write_text("")

        record: list[tuple[str, set[str]]] = []
        ok = distributor.run_dependency_aware(
            session, launch_fn=_autocomplete_launcher(record), poll_interval=0.01,
        )

        assert ok is True
        assert [aid for aid, _ in record] == ["agent_002"]
