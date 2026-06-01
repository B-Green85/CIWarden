"""Conductor session state — the agents, paths, and identifiers for one generation."""

from __future__ import annotations

import json
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# Spec-mandated staging location; tempfile.gettempdir() resolves to /tmp on POSIX
# while staying portable and avoiding a hardcoded /tmp literal.
DEFAULT_STAGING_ROOT = Path(tempfile.gettempdir()) / "conductor_staging"
DEFAULT_VDB_ROOT = Path(".cdmad/vdb")

# Persisted under staging_root so ``--resume`` can rebuild the session (notably the
# repo_root, which is not otherwise recoverable from the per-agent staging files).
SESSION_MANIFEST = "session.json"


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

    def save_manifest(self) -> Path:
        """Persist this session to ``<staging_root>/session.json`` for ``--resume``."""
        self.staging_root.mkdir(parents=True, exist_ok=True)
        path = self.staging_root / SESSION_MANIFEST
        data = {
            "repo_root": str(self.repo_root),
            "repo_name": self.repo_name,
            "session_id": self.session_id,
            "vdb_root": str(self.vdb_root),
            "staging_root": str(self.staging_root),
            "agents": [
                {
                    "agent_id": a.agent_id,
                    "description": a.description,
                    "prompt": a.prompt,
                    "module_key": a.module_key,
                    "dependencies": a.dependencies,
                }
                for a in self.agents
            ],
        }
        path.write_text(json.dumps(data, indent=2))
        return path

    @classmethod
    def from_manifest(cls, staging_root: Path) -> ConductorSession:
        """Rebuild a session from ``<staging_root>/session.json`` (raises if absent)."""
        data = json.loads((staging_root / SESSION_MANIFEST).read_text())
        agents = [
            AgentSpec(
                a["agent_id"],
                a["description"],
                a["prompt"],
                a["module_key"],
                list(a.get("dependencies", [])),
            )
            for a in data["agents"]
        ]
        return cls(
            repo_root=Path(data["repo_root"]),
            repo_name=data["repo_name"],
            agents=agents,
            session_id=data["session_id"],
            vdb_root=Path(data["vdb_root"]),
            staging_root=Path(data["staging_root"]),
        )
