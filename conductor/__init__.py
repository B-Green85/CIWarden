"""Conductor — multi-agent orchestration with atomic commit (GateChain v2).

First and last authority over a multi-agent generation session: reads prompts, primes
the VDB, distributes to agents, owns the staging directory, proofs all output before a
single file reaches the repo, and issues an atomic commit only when all agents are clean.
"""

from __future__ import annotations

from conductor.cli import main, run_session
from conductor.session import AgentSpec, ConductorSession

__all__ = ["AgentSpec", "ConductorSession", "main", "run_session"]
