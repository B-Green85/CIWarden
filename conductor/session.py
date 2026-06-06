"""Conductor session state — the agents, paths, and identifiers for one generation."""

from __future__ import annotations

import json
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# Spec-mandated staging base; tempfile.gettempdir() resolves to /tmp on POSIX while
# staying portable and avoiding a hardcoded /tmp literal. Resolved once here so the
# canonical absolute path (macOS maps /var/folders → /private/var/...) is stable and
# can be persisted to the manifest, rather than recomputed per-process where $TMPDIR
# drift would make the Conductor and a later --resume disagree on the path.
DEFAULT_STAGING_ROOT = (Path(tempfile.gettempdir()) / "conductor_staging").resolve()
DEFAULT_VDB_ROOT = Path(".cdmad/vdb")

# The manifest lives at the staging *base* (not the namespaced per-session root) so
# ``--resume`` can find it without already knowing the session_id. It records repo_root
# plus the resolved staging paths, none of which are recoverable from the per-agent
# staging files alone.
SESSION_MANIFEST = "session.json"

# Sentinel default for ConductorSession.staging_root: when left unset, the staging root
# is derived as ``staging_base / repo_name / session_id`` (see __post_init__). Tests and
# callers may still pass an explicit staging_root to pin an isolated directory.
_DERIVE_STAGING_ROOT = Path("__derive_staging_root__")


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
    staging_base: Path = DEFAULT_STAGING_ROOT
    staging_root: Path = _DERIVE_STAGING_ROOT

    def __post_init__(self) -> None:
        # Namespace the per-session staging root by repo + session unless one was passed
        # explicitly. Keeping agent dirs under <base>/<repo>/<session>/<agent_id> stops a
        # stale or parallel session from colliding on a bare agent_id dir — the Phase 4
        # ".done landed in the wrong staging dir" failure.
        if self.staging_root is _DERIVE_STAGING_ROOT:
            self.staging_root = self.staging_base / self.repo_name / self.session_id

    @property
    def repo_vdb_dir(self) -> Path:
        return self.vdb_root / self.repo_name

    def agent_by_id(self, agent_id: str) -> AgentSpec | None:
        return next((a for a in self.agents if a.agent_id == agent_id), None)

    def save_manifest(self) -> Path:
        """Persist this session to ``<staging_base>/session.json`` for ``--resume``.

        Written at the base (not the namespaced staging_root) so resume can locate it
        without the session_id. Records the resolved staging_base and staging_root so
        resume reuses the exact paths instead of recomputing tempfile.gettempdir(),
        which can drift across processes via $TMPDIR.
        """
        self.staging_base.mkdir(parents=True, exist_ok=True)
        path = self.staging_base / SESSION_MANIFEST
        data = {
            "repo_root": str(self.repo_root),
            "repo_name": self.repo_name,
            "session_id": self.session_id,
            "vdb_root": str(self.vdb_root),
            "staging_base": str(self.staging_base),
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
    def from_manifest(cls, staging_base: Path) -> ConductorSession:
        """Rebuild a session from ``<staging_base>/session.json`` (raises if absent).

        Reuses the persisted (resolved) staging_root verbatim so the rebuilt session
        watches the exact paths the prompts were baked with — never a recomputed temp
        dir. Falls back gracefully for manifests written before staging_base existed.
        """
        data = json.loads((staging_base / SESSION_MANIFEST).read_text())
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
            staging_base=Path(data.get("staging_base", staging_base)),
            staging_root=Path(data["staging_root"]),
        )
