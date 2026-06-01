"""Conductor session state — the agents, paths, and identifiers for one generation."""

from __future__ import annotations

import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# Spec-mandated staging location; tempfile.gettempdir() resolves to /tmp on POSIX
# while staying portable and avoiding a hardcoded /tmp literal.
DEFAULT_STAGING_ROOT = Path(tempfile.gettempdir()) / "conductor_staging"
DEFAULT_VDB_ROOT = Path(".cdmad/vdb")


def module_key_from_description(description: str) -> str:
    """Derive a schema/VDB module key from a short description: lowercase, spaces→underscores."""
    return "_".join(description.lower().split())


@dataclass
class AgentSpec:
    """One agent's assignment within a session."""

    agent_id: str               # e.g. "agent_001"
    description: str            # short human label, e.g. "memory allocator"
    prompt: str                 # the generation prompt
    module_key: str             # schema/VDB key, derived from description, e.g. "memory_allocator"
    dependencies: list[str] = field(default_factory=list)  # module_keys this agent depends on

    def staging_dir(self, staging_root: Path) -> Path:
        return staging_root / self.agent_id


@dataclass
class ConductorSession:
    """All state for a single multi-agent generation session."""

    repo_root: Path
    repo_name: str
    agents: list[AgentSpec]
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    vdb_root: Path = DEFAULT_VDB_ROOT
    staging_root: Path = DEFAULT_STAGING_ROOT

    @property
    def repo_vdb_dir(self) -> Path:
        return self.vdb_root / self.repo_name

    def agent_by_id(self, agent_id: str) -> AgentSpec | None:
        return next((a for a in self.agents if a.agent_id == agent_id), None)
