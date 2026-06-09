"""Dependency-graph construction for the Conductor.

Parses the ``CONTRACTS_CONSUMED`` block out of each agent prompt and builds a DAG:
one node per agent, one edge per declared dependency. The graph drives launch
ordering (an agent waits until every dependency has signalled ``.done``) and commit
ordering (dependencies commit before dependents).

This module is pure: it reads prompt files and returns data structures. It has no
side effects — no writes, no network, no process launches.

A prompt with no ``CONTRACTS_CONSUMED`` block declares no dependencies, so the agent
is a root node. A session in which *every* prompt lacks the block therefore yields a
graph with no edges, and ``ready_agents`` returns all agents from the first call —
which is exactly today's "launch everyone at once" behaviour. The upgrade is invisible
to sessions that don't use it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class DependencyNode:
    """One agent's position in the dependency graph.

    ``depends_on`` and ``required_by`` hold ``agent_id`` strings (the keys of the
    ``agent_prompts`` mapping passed to :meth:`DAGBuilder.build`), so the node is
    JSON-serialisable as-is — only strings and lists of strings.
    """

    agent_id: str
    depends_on: list[str] = field(default_factory=list)   # agent_ids this agent waits for
    required_by: list[str] = field(default_factory=list)  # agent_ids waiting on this one


class DAGBuilder:
    """Builds a dependency graph from agent prompt files.

    Parses ``CONTRACTS_CONSUMED`` blocks to extract inter-agent dependencies declared
    via the ``(Agent N)`` notation.
    """

    # Matches the dependency declaration "(Agent 3)" anywhere in a consumed line.
    AGENT_REF_PATTERN = re.compile(r"\(Agent\s+(\d+)\)", re.IGNORECASE)
    # Pulls the trailing integer out of an agent_id ("agent_001" → 1, "agent_3" → 3),
    # so a "(Agent N)" reference resolves regardless of zero-padding in the id.
    _AGENT_ID_NUM = re.compile(r"(\d+)\s*$")

    def build(self, agent_prompts: dict[str, Path]) -> dict[str, DependencyNode]:
        """Build the graph.

        ``agent_prompts``: ``{agent_id: path_to_prompt_file}``.
        Returns ``{agent_id: DependencyNode}``.
        """
        nodes: dict[str, DependencyNode] = {
            agent_id: DependencyNode(agent_id=agent_id)
            for agent_id in agent_prompts
        }

        num_to_id = self._number_index(agent_prompts)

        for agent_id, prompt_path in agent_prompts.items():
            deps = self._extract_dependencies(prompt_path, num_to_id, agent_id)
            nodes[agent_id].depends_on = deps
            for dep_id in deps:
                # dep_id is guaranteed present in nodes (it came from num_to_id, which
                # is built only from agent_prompts keys), but guard anyway so a future
                # caller passing a hand-built index can't trip an IndexError here.
                if dep_id in nodes:
                    nodes[dep_id].required_by.append(agent_id)

        self._validate(nodes)
        return nodes

    def _number_index(self, agent_prompts: dict[str, Path]) -> dict[int, str]:
        """Map each agent's trailing number to its agent_id.

        The wizard emits zero-padded ids ("agent_001"), while the ``(Agent N)`` notation
        in prompts is unpadded. Resolving via the trailing integer bridges the two so a
        "(Agent 1)" reference finds "agent_001". If two ids share a number (they should
        not within one session) the lexically-first id wins, for determinism.
        """
        index: dict[int, str] = {}
        for agent_id in sorted(agent_prompts):
            match = self._AGENT_ID_NUM.search(agent_id)
            if match is None:
                continue
            num = int(match.group(1))
            index.setdefault(num, agent_id)
        return index

    def _extract_dependencies(
        self, prompt_path: Path, num_to_id: dict[int, str], self_id: str
    ) -> list[str]:
        """Read the prompt and extract the agent_ids referenced in CONTRACTS_CONSUMED.

        Only the ``CONTRACTS_CONSUMED`` section is scanned, so a "(Agent N)" appearing
        in prose elsewhere in the prompt is ignored. A reference to an agent that is not
        part of this session, or a self-reference, is silently skipped rather than
        raising — the operator's prompt may name agents from a larger plan than the
        subset actually launched.
        """
        text = prompt_path.read_text()

        consumed_match = re.search(
            r"CONTRACTS_CONSUMED:(.*?)(?:MODIFIES_EXISTING:|CONTRACTS_PRODUCED:|$)",
            text,
            re.DOTALL | re.IGNORECASE,
        )
        if not consumed_match:
            return []

        consumed_section = consumed_match.group(1)

        deps: list[str] = []
        for num_str in self.AGENT_REF_PATTERN.findall(consumed_section):
            dep_id = num_to_id.get(int(num_str))
            if dep_id is None or dep_id == self_id or dep_id in deps:
                continue
            deps.append(dep_id)
        return deps

    def _validate(self, nodes: dict[str, DependencyNode]) -> None:
        """Validate the DAG: every node reachable, no cycles.

        Raises ``ValueError`` with a readable message naming the back-edge if a cycle is
        found, so the operator can locate the offending ``CONTRACTS_CONSUMED`` blocks.
        """
        visited: set[str] = set()
        in_stack: set[str] = set()

        def dfs(node_id: str) -> None:
            visited.add(node_id)
            in_stack.add(node_id)
            for dep in nodes[node_id].depends_on:
                if dep not in visited:
                    dfs(dep)
                elif dep in in_stack:
                    msg = (
                        f"Dependency cycle detected: {node_id} → {dep}. "
                        f"Check CONTRACTS_CONSUMED blocks for circular references."
                    )
                    raise ValueError(msg)
            in_stack.discard(node_id)

        for node_id in nodes:
            if node_id not in visited:
                dfs(node_id)

    def topological_order(self, nodes: dict[str, DependencyNode]) -> list[str]:
        """Return agent_ids in topological order — dependencies before dependents.

        Within a wave (nodes whose dependencies are all satisfied) ids are emitted in
        sorted order, so the ordering is deterministic across runs. Used by commit
        ordering to ensure a producer commits before its consumers.
        """
        in_degree = {nid: len(n.depends_on) for nid, n in nodes.items()}
        queue = [nid for nid, deg in in_degree.items() if deg == 0]
        order: list[str] = []

        while queue:
            queue.sort()  # deterministic ordering within a wave
            node_id = queue.pop(0)
            order.append(node_id)
            for dependent in nodes[node_id].required_by:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        if len(order) != len(nodes):
            msg = "DAG validation failed — cycle present despite earlier check."
            raise ValueError(msg)

        return order

    def ready_agents(
        self, nodes: dict[str, DependencyNode], done: set[str]
    ) -> list[str]:
        """Return agent_ids that are ready to launch.

        An agent is ready when it is not yet done and every dependency is in ``done``.
        With no dependencies declared, every not-yet-done agent is ready immediately —
        the backward-compatible "launch everyone" path.
        """
        return [
            nid for nid, node in nodes.items()
            if nid not in done
            and all(dep in done for dep in node.depends_on)
        ]
