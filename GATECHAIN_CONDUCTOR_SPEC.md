# GateChain + Conductor — Consolidated Spec

**Copyright (c) 2026 TrueSystems LLC. All rights reserved.**
**Status:** Authoritative — ready for implementation
**Version:** 2.0 (consolidates the original spec + signed-off addendum)
**Date:** May 31, 2026

> This document supersedes the original v1 spec and its addendum. All addendum
> corrections are applied inline. There is no separate addendum to cross-reference.

---

## Overview

This spec covers two interrelated upgrades to the GateChain system:

**1. Memory Gate — Remove LLM extraction entirely.** Replace the Haiku API call with
`SchemaCaptureClient` — a static AST-based extractor with no network dependency, no
latency, and no API cost. Implement the `VectorContractStore` scaffold that has been
stubbed since v1.0.0. Scope contracts per repo, per session.

**2. Conductor — Multi-agent orchestration with atomic commit.** The `conductor/`
package becomes the first and last authority over every multi-agent generation
session. It reads all prompts, primes the VDB from the existing codebase, distributes
to agents, owns the staging directory, proofs all output before a single file reaches
the repo, and issues an atomic commit only when all agents are clean. The gate chain
then runs as normal.

These two upgrades are designed together. The Conductor writes to the VDB. The memory
gate reads from it. The drift scorer and gate chain are untouched.

---

## What This Replaces

| Before | After |
|---|---|
| Haiku API call (~70s per commit) | `SchemaCaptureClient` — AST scan, ~500ms, no API call |
| `VectorContractStore` all `NotImplementedError` | Fully implemented, JSON VDB, repo+session scoped |
| No multi-agent coordination layer | Conductor owns staging, proofing, VDB, repo copy |
| Agents write to repo directly | Agents write to `/tmp/conductor_staging/` only |
| `anthropic` in `requirements.txt` | Removed — after the import chain is made lazy |
| Inter-session drift only | Inter-session (gate) + intra-session (peer checker) |
| Memory gate is the contract *writer* | Conductor writes in managed runs; gate reads (see Memory Gate §) |

---

## Why Atomic Commit Hardens the System

Before this update, the gate chain was hard on individual commits but had no concept
of multi-agent session coherence. An agent could pass every gate — lint, typecheck,
security, memory, stress, build — and still ship code that quietly contradicts what
another agent built in the same session. Gates checked each commit in isolation.

The atomic commit closes that gap. Nothing lands until the Conductor has verified the
entire generation is internally consistent:

- Every interface declared was actually delivered
- Every `consumes` was matched against the corresponding `exposes`
- No symbol collisions across subsystem boundaries
- The prior session's contracts were honored or a breaking change was explicitly declared

The gate chain was already making individual commits ungameable. This makes the
**session** ungameable. The VDB compounds over phases — by Phase 4, the Conductor has
a complete contract history going back to the first line of boot.asm.

> **`consumes` enforcement is a Tier 1 (Conductor) responsibility.** See the
> Two-Tier Drift Detection section and the `models.py` design decision for the exact
> boundary — the solo CI path does not cross-validate `consumes`.

---

## Architecture

### Session Flow

```
Operator runs: python3 -m conductor
        │
        ▼
[SETUP WIZARD]  (conductor/cli.py)
  How many agents? (1–12)
  Per-agent: subsystem path + prompt
  Confirm launch
        │
        ▼
[CONDUCTOR INITIALIZES]
  1. Read all agent prompts
  2. Extract dependency graph from prompts
  3. Write dependency_graph.json to VDB
  4. Scan existing codebase (if VDB empty) → prime VDB
  5. Write expected_interfaces.json to VDB
  6. Stamp each agent's schema with its module_key
        │
        ▼
[CONDUCTOR DISTRIBUTES]  (conductor/distributor.py)
  Agent 1 → /tmp/conductor_staging/agent_001/src/boot/
  Agent 2 → /tmp/conductor_staging/agent_002/src/memory/
  Agent 3 → /tmp/conductor_staging/agent_003/src/scheduler/
  ...
  Each agent writes .done when complete
        │
        ▼
[CONDUCTOR PROOFS — dependency order]  (conductor/prover.py)
  Parse agent_006/ (Sentinel — no deps)
  Parse agent_001/ (Boot — no deps)
  Parse agent_002/ (Memory — no deps)
  Parse agent_005/ (FS — depends on Memory)
  Parse agent_003/ (Scheduler — depends on Memory)
  Parse agent_004/ (Syscall — depends on Scheduler)
        │
        ├── Conflict found at agent_003/
        │     Write .conflict_report.txt to staging
        │     Agent 3 reads report, rewrites, writes .done
        │     Conductor re-parses agent_003/ only
        │     Report escalates on each retry (more specific each time)
        │     No retry limit — Agent 3 stays after class
        │
        ▼
[ALL AGENTS CLEAN — peer checker reports no conflicts]  (conductor/commit.py)
  1. commit_session() writes the VDB session file atomically
       session_{id}.json written + index.json updated + live/ cleared
  2. Copy all staged files from /tmp/conductor_staging/ → worktree
  3. Enqueue ONE queue entry (full file list, single message)
  4. Delete /tmp/conductor_staging/
        │
        ▼
[QUEUE WORKER]  (queue/commit_queue.py — existing)
  git add -- <files>  +  git commit --no-verify  →  calls orchestrator
        │
        ▼
[GATE CHAIN RUNS — existing orchestrator behavior]
  lint → typecheck → security → memory → test → stress → build
  Memory gate runs in MANAGED mode — reads committed VDB (session N vs N-1)
  Merge token issued on all-pass
```

**Critical ordering.** `commit_session()` MUST complete before the gate chain runs.
The managed-mode memory gate reads the committed VDB; if the VDB write slips after the
gate run, the gate reads stale state and inter-session drift scoring is wrong.

### Two-Tier Drift Detection

**Tier 1 — Intra-session peer check (Conductor, staging)**
- Runs during proofing phase, before anything touches the repo
- Detects: symbol collisions, interface mismatches, assumption clashes
- Resolution: targeted conflict report → agent retry
- No gate chain involvement
- **Owns `consumes` enforcement** — symbol- and signature-level

**Tier 2 — Inter-session gate check (memory gate, existing)**
- Runs at commit time via the queue → orchestrator
- Compares committed session N against session N-1 in VDB
- Drift score 0.0–1.0, threshold 0.5 (existing behavior)
- Scores `contracts`, `assumptions`, `dependencies` — does **not** see `consumes`

> **Known and accepted limitation.** Tier 2 does not enforce `consumes` matching. The
> guarantee that "every `consumes` is matched against the corresponding `exposes`" is
> enforced entirely by Tier 1. An agent that runs the solo CI path (no Conductor) has
> its architectural contracts scored against the prior session, but its `consumes`
> declarations are not cross-validated. This is an accepted limitation of the solo path.

---

## File Map

| Path | Action | Notes |
|---|---|---|
| `conductor/` | **NEW PACKAGE** | Greenfield build, ~600–900 LOC across 7 files |
| `conductor/__init__.py` | NEW | Package entry point (`python3 -m conductor`) |
| `conductor/cli.py` | NEW | Setup wizard — agent count, subsystem paths, prompts, confirm |
| `conductor/session.py` | NEW | `ConductorSession` dataclass + state management |
| `conductor/vdb_io.py` | NEW | `load_vdb_index`, `write_session_vdb`, `prime_vdb_from_codebase`, `_get_repo_name` |
| `conductor/distributor.py` | NEW | Staging dir layout, prompt injection, `module_key` stamp, `.done` polling |
| `conductor/prover.py` | NEW | Parse staging in dependency order, drive `PeerChecker`, escalate reports |
| `conductor/commit.py` | NEW | Atomic commit: `commit_session` → copy to worktree → enqueue ONE queue entry |
| `memory/llm_client.py` | MODIFY | Make `import anthropic` lazy (gate for anthropic removal) |
| `memory/schema_capture.py` | NEW | `SchemaCaptureClient` — schema-first, AST fallback |
| `memory/contract_store.py` | MODIFY | Implement `VectorContractStore` — all 9 abstract + 4 new methods |
| `memory/peer_checker.py` | NEW | Intra-session cross-check |
| `memory/memory_gate.py` | MODIFY | Add solo/managed mode branch |
| `memory/app.py` | MODIFY | Wire client + store selection + `managed` flag |
| `memory/models.py` | NO CHANGE | Explicit decision — Tier 1 owns `consumes` (see design decision) |
| `scripts/prime_vdb.py` | NEW | Thin CLI wrapper around `conductor/vdb_io.py` |
| `scripts/migrate_contracts.py` | NEW | One-time JSON store → VDB migration |
| `tests/memory/__init__.py` | NEW | Package dir does not exist yet |
| `tests/memory/test_schema_capture.py` | NEW | |
| `tests/memory/test_vector_store.py` | NEW | |
| `tests/memory/test_peer_checker.py` | NEW | |
| `tests/memory/test_migration.py` | NEW | |
| `tests/memory/test_memory_gate.py` | MODIFY | Parametrize over both backends |
| `requirements.txt` | MODIFY | Remove `anthropic` — only after `llm_client.py` is lazy |
| `.env.example` | MODIFY | Add new vars, retain `CDMAD_LOCAL_DEV` |

**Zero changes to:** `drift_scorer.py`, `models.py`, `orchestrator/`, the pre-commit
hook, other gates (lint, typecheck, security, test, stress, build), and
`queue/commit_queue.py`.

---

## Implementation Detail

### 1. `conductor/` — NEW PACKAGE

There is no pre-existing conductor file in the repo. The conductor is a new package of
approximately **600–900 lines** across seven files. It is **not** an edit to an
existing module.

The package owns the full multi-agent lifecycle:

| Component | File | Responsibility |
|---|---|---|
| Setup wizard | `cli.py` | Agent count (1–12), per-agent subsystem path + prompt, confirm launch |
| Session state | `session.py` | `ConductorSession` dataclass; tracks agents, staging root, session id |
| VDB I/O | `vdb_io.py` | `load_vdb_index`, `write_session_vdb`, `prime_vdb_from_codebase`, `_get_repo_name` |
| Distribution | `distributor.py` | Staging layout, prompt injection, `module_key` stamp, `.done` polling, `.conflict_report.txt` write |
| Proofing | `prover.py` | Dependency-order parse, drive `PeerChecker`, dependency graph + expected interfaces, escalating reports |
| Atomic commit | `commit.py` | `commit_session` → copy staged files to worktree → enqueue ONE queue entry |

**Startup VDB prime** (`vdb_io.py`). First thing the Conductor does after
initialization, before distributing prompts. Only fires on cold start (no existing
session for this repo).

```python
async def prime_vdb_from_codebase(session: ConductorSession) -> None:
    """
    Scan existing repo, extract contracts via SchemaCaptureClient AST,
    write to VDB as the baseline prior session.
    Skipped if VDB already has a session for this repo.
    """
    index = load_vdb_index(session)
    if index.get("latest_session"):
        log("VDB already primed — skipping codebase scan")
        return

    log("CONDUCTOR  ● priming VDB from existing codebase...")
    source_files = _discover_source_files()
    client = SchemaCaptureClient()
    contracts = {}
    for module, code in source_files.items():
        summary = await client.extract_contracts(code, module, "prime_000", 0)
        contracts[module] = summary.model_dump()
    write_session_vdb(session, contracts)
    log(f"CONDUCTOR  ✓ VDB primed — {len(contracts)} modules indexed")
```

**Dependency graph extraction** (`prover.py`). After reading all agent prompts, before
distributing: parse each prompt for subsystem ownership, expected deliverables, and
dependencies; determine parse order (no-dependency subsystems first); write
`.cdmad/vdb/{repo}/dependency_graph.json`. The graph drives proofing parse order —
extracted from prompts, never hardcoded.

**Expected interfaces pre-validation** (`prover.py`). After reading prompts, write
`expected_interfaces.json`: what each agent *should* build, extracted from their
prompt's deliverables. Actual generation is scored against this in addition to the peer
cross-check. An agent that delivers less than promised is flagged, not just one that
contradicts a peer.

**`consumes` field in agent schema** (`prover.py` + `distributor.py`). The prompt
template instructs agents to include a `consumes` block in their `session_schema.json`.
`PeerChecker` uses it to detect `INTERFACE_MISMATCH` — signature-level mismatches, not
just symbol collisions.

**Escalating conflict reports** (`prover.py`). `send_conflict_report` passes `attempt`
to `PeerChecker.format_conflict_report`:
- Attempt 1: what's wrong and why
- Attempt 2: what's wrong, why, exact correct signature
- Attempt 3+: what's wrong, why, correct signature, exact code block

No retry limit. The agent works until it's right.

**`.done` / `.conflict_report.txt` protocol** (`distributor.py`).

```python
DONE_FLAG = ".done"
CONFLICT_REPORT_FILE = ".conflict_report.txt"
```

Conductor monitors for `.done` to detect completion; writes `.conflict_report.txt` to
an agent's staging dir on conflict. The agent reads the report, addresses every point,
rewrites, and writes `.done` again.

---

### 2. `memory/llm_client.py` — MODIFY (lazy anthropic import)

`memory/llm_client.py` currently has an unconditional top-level `import anthropic`.
That module is imported transitively by `memory_gate.py`, `app.py`, and the new
`schema_capture.py` (which imports `LLMClient` from it). Removing the `anthropic`
package without fixing this import produces a `ModuleNotFoundError` that breaks the
entire memory gate at load time.

**Fix — make the import lazy inside `AnthropicClient`:**

```python
class AnthropicClient(LLMClient):
    def __init__(self, api_key=None, model="claude-haiku-4-5-20251001"):
        import anthropic  # lazy — only when AnthropicClient is actually used
        self._anthropic = anthropic
        ...
```

This preserves `AnthropicClient` as a commented re-enable path. `httpx` remains a
top-level import and stays in `requirements.txt`. Removing `anthropic` from
`requirements.txt` is safe **only after** this fix lands — a hard ordering constraint.

---

### 3. `memory/schema_capture.py` — NEW

Replaces `AnthropicClient` as the extraction layer. Implements the `LLMClient`
interface. Two strategies, tried in order.

**Strategy 1 — Agent-declared schema.** Agent writes `.cdmad/session_schema.json`
during generation. `SchemaCaptureClient` reads it directly. Primary path for all
Conductor-managed sessions.

**Strategy 2 — AST fallback.** No schema file present. Fires when:
- Existing codebase with no prior Conductor session (Phase 2 cold start)
- Agent ran outside Conductor (non-managed session)

AST fallback uses Python `ast` for `.py` files and regex for `.rs` files. This is the
canonical static-analysis implementation — `conductor/` imports from here, not the
other way around.

```python
class SchemaCaptureClient(LLMClient):
    """
    Reads contracts from agent-declared session_schema.json,
    or falls back to AST/static analysis if no schema is present.
    No LLM API call. No network. No latency.
    """
    def __init__(
        self,
        schema_path: str | Path | None = None,
        repo_name: str | None = None,
    ) -> None:
        self.schema_path = Path(schema_path or ".cdmad/session_schema.json")
        self.repo_name = repo_name or _get_repo_name()   # imported from conductor.vdb_io

    async def extract_contracts(
        self,
        source_code: str,
        module_name: str,
        generation_id: str,
        sequence: int,
    ) -> ContractSummary:
        if self.schema_path.exists():
            return self._from_schema(module_name, generation_id, sequence)
        return self._from_ast(source_code, module_name, generation_id, sequence)

    def _from_schema(self, module_name, generation_id, sequence) -> ContractSummary:
        """
        Read from agent-declared session_schema.json.
        Resolve the module entry by `module_key` when present (Conductor-stamped),
        falling back to module_name when absent (solo CI path, Python repos).
        """
        ...

    def _from_ast(self, source_code, module_name, generation_id, sequence) -> ContractSummary:
        """
        Static analysis fallback.
        Python: ast module — classes, functions, imports.
        Rust: regex — pub fn, pub struct, pub trait, pub enum, use statements,
              assumption comments (// must, // requires, // assumes).
        """
        ...
```

**Module-key resolution.** The memory gate discovers modules by top-level directory
(`gates`, `orchestrator`, `memory`, …). A foreign target such as a Rust kernel uses
subsystem directories (`src/boot/`, `src/scheduler/`, …) that do not align. The
Conductor therefore stamps each agent's schema with an explicit `module_key`;
`_from_schema` resolves the module entry by that key when present, falling back to the
discovered directory name when absent.

**Agent-declared schema format** (written by agent to `.cdmad/session_schema.json`):

```json
{
  "module_key": "scheduler",
  "repo": "GolemLinux",
  "session_id": "abc123",
  "modules": {
    "scheduler": {
      "contracts": [
        {
          "type": "function",
          "name": "init",
          "fields": [],
          "consumed_by": ["kernel_main"],
          "methods": []
        }
      ],
      "exposes": [
        {
          "symbol": "scheduler::init",
          "signature": "pub fn init() -> Result<(), KernelError>",
          "assumptions": ["memory initialized before call", "no_std environment"]
        }
      ],
      "consumes": [
        {
          "symbol": "memory::init",
          "expected_signature": "pub fn init(memory_map: *const ()) -> Result<(), KernelError>",
          "consumed_in": "src/scheduler/mod.rs"
        }
      ],
      "assumptions": [
        "x86_64 System V ABI",
        "callee-saved registers: rbx, rbp, r12, r13, r14, r15",
        "no_std"
      ],
      "dependencies": ["core", "alloc"]
    }
  }
}
```

> `exposes`/`consumes` are rich objects here, used by `PeerChecker` directly. When
> `_from_schema` produces a `ContractSummary`, `exposes` is flattened to `list[str]`
> and `consumes` is dropped — see the `models.py` design decision. This is intentional.

---

### 4. `memory/contract_store.py` — MODIFY (implement `VectorContractStore`)

The abstract interface (`BaseContractStore`) is unchanged. `ContractStore` (JSON file
store) is unchanged. This is additive only.

`BaseContractStore` declares **9** abstract methods. A subclass that does not implement
all 9 remains abstract and raises `TypeError` at construction. `VectorContractStore`
must implement all 9, plus 4 new methods, plus the non-abstract helpers the gate path
touches.

**Storage layout:**

```
.cdmad/vdb/
  {repo_name}/
    index.json                       latest_session, previous_session pointers
    expected_interfaces.json         Conductor pre-validation baseline
    dependency_graph.json            parse order + dependency map
    session_{session_id}.json        committed generation N
    session_{prev_id}.json           committed generation N-1
    session_state.json               VDB-backend session state
    checkpoints/
      checkpoint_{session}_{seq}.json
    live/
      {agent_id}__{module}.json      in-progress (keyed by agent_id AND module)
      {agent_id}__{module}.json      cleared on atomic commit
```

> **`live/` keying.** Live schemas are keyed by **(agent_id, module)** —
> `live/{agent_id}__{module}.json`. The gate's `_execute` loops over modules; an
> agent-only key would cause every module to overwrite the same file. Same applies to a
> single agent producing multiple modules.

**Constructor:**

```python
class VectorContractStore(BaseContractStore):
    def __init__(
        self,
        repo_name: str,
        vdb_root: str | Path = ".cdmad/vdb",
        session_id: str | None = None,
    ) -> None:
        self.repo_name = repo_name
        self.repo_dir = Path(vdb_root) / repo_name
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        (self.repo_dir / "live").mkdir(exist_ok=True)

    @property
    def root(self) -> Path:
        """Alias for repo_dir — keeps MemoryGate._cache_path consistent across backends."""
        return self.repo_dir
```

**All 9 abstract methods — must be implemented:**

`save_contract(summary)` — writes to `live/{agent_id}__{module}.json` during a session.
Pending state — not committed to the session file until `commit_session()`.

`load_latest_contract(module)` — reads `index.json → latest_session → session file →
module entry`. Falls back to scanning session files if the index is stale.

`load_previous_contract(module, before_sequence)` — reads `index.json →
previous_session`. Enables inter-session drift scoring. Existing `DriftScorer` behavior
unchanged.

`list_contracts(module)` — returns all contracts for a module ordered by sequence
(scans session files). **Required** — it is abstract; omitting it leaves the class
abstract.

`save_checkpoint(checkpoint)` — writes to `{repo_dir}/checkpoints/`.

`load_latest_checkpoint(session_id)` — reads the most recent checkpoint for a session.

`get_or_create_session()` — reads or creates `{repo_dir}/session_state.json`.

`save_session(session)` — persists session state.

`advance_session(session)` — increments sequence, updates generation ID.

**4 new methods:**

`commit_session(contracts)` — atomic write. All contracts for the session written to
`session_{session_id}.json` in one operation. Index updated. Live schemas cleared.
Called by the Conductor after all agents are clean — and, in the solo CI path, once by
the gate at the end of its module loop (see Memory Gate §). Never called per-module
mid-loop.

`retrieve_by_files(file_paths)` — given file paths touched in a commit, return only the
contracts those files defined or consumed. Bounds context at scale. Reads from
`index.json → latest_session`. Key for the future ChromaDB swap.

`write_expected_interfaces(interfaces)` — Conductor calls on startup after the codebase
scan. Writes `expected_interfaces.json`. All generation scored against this baseline.

`write_dependency_graph(graph)` — Conductor calls after reading agent prompts. Writes
`dependency_graph.json`. Used by the peer checker for parse order.

**Non-abstract helpers to port from `ContractStore`:**

`load_session()` — used by `get_or_create_session` and the cache path.
`list_modules()` — VDB equivalent; used implicitly during module discovery.

---

### 5. `memory/peer_checker.py` — NEW

Intra-session cross-check. Called by the Conductor after parsing each agent's staging
output. Also callable standalone for incremental checks during generation. Operates on
the raw live-schema dicts (which carry full `exposes`/`consumes` objects), so it sees
signature-level detail the `ContractSummary` does not.

```python
@dataclass
class Conflict:
    kind: Literal["COLLISION", "INTERFACE_MISMATCH", "ASSUMPTION_CLASH"]
    agent_id: str           # agent with the problem
    peer_id: str            # peer it conflicts with
    symbol: str             # symbol in question
    agent_signature: str    # what the agent declared
    peer_signature: str     # what the peer declared
    detail: str             # human-readable explanation
    fix: str                # exact fix instruction


class PeerChecker:
    """
    Cross-checks agent contracts against peer agents' live schemas.

    Conflict types:
      COLLISION          — same symbol exposed by two agents
      INTERFACE_MISMATCH — agent consumes symbol with wrong signature
      ASSUMPTION_CLASH   — contradicting assumptions about shared state
    """
    def __init__(self, store: VectorContractStore) -> None:
        self.store = store

    def check(self, agent_id: str, contracts: dict) -> list[Conflict]:
        """Check agent against all peers already in live/."""
        ...

    def read_all_live(self) -> dict[str, dict]:
        """Read all live schema files from VDB (keyed by agent_id__module)."""
        ...

    def format_conflict_report(
        self,
        agent_id: str,
        subsystem: str,
        conflicts: list[Conflict],
        attempt: int,
        staging_dir: Path,
    ) -> str:
        """
        Escalates specificity by attempt number:
          Attempt 1: what's wrong and why
          Attempt 2: + exact correct signature
          Attempt 3+: + exact code block
        """
        ...
```

**Conflict report format:**

```
CONDUCTOR — CONFLICT REPORT (Attempt {N})
Agent {id} | Subsystem: {subsystem}
Timestamp: {iso}

CONFLICT DETECTED:
  Type: INTERFACE_MISMATCH
  Your consumes:
    memory::init(memory_map: *const ()) -> Result<(), &'static str>
  Agent 002 (src/memory/) currently exposes:
    memory::init(memory_map: *const ()) -> Result<(), KernelError>
  Conflict: return type mismatch (&'static str vs KernelError)

FIX REQUIRED:
  1. Align return type to Result<(), KernelError>
  2. Import KernelError — defined in src/kernel/error.rs (Agent 002 owns this)
  3. Update your consumes declaration in session_schema.json to match

[Attempt 2+: correct signature shown explicitly]
[Attempt 3+: exact code block provided]

Rewrite affected file(s) and resubmit to:
  /tmp/conductor_staging/agent_003/

Write .done when complete.
```

---

### 6. `memory/memory_gate.py` — MODIFY (solo/managed mode branch)

The gate today is the contract **writer**: `_execute` does `get_or_create_session` →
`advance_session` → per-module `extract_contracts` → `save_contract` →
`load_previous_contract` → `save_checkpoint`. The new model makes the Conductor the
writer. These cannot coexist with `_execute` untouched. `_execute` gains an explicit
mode branch, selected by `CDMAD_MANAGED`.

**Solo mode** (default CI path; no Conductor; `CDMAD_MANAGED` unset):
- Extract via `SchemaCaptureClient`.
- Buffer all module summaries across the module loop (the loop already builds
  `all_summaries`).
- After the loop, call `commit_session(all_summaries)` **once** for the VDB backend —
  not per-module `save_contract`.
- Then call `load_previous_contract` to read committed N-1 and score.
- The JSON backend keeps existing per-module `save_contract` behavior, unchanged.

**Managed mode** (`CDMAD_MANAGED=1`; Conductor session active):
- The Conductor already wrote and committed the VDB during proofing.
- The gate is **read-only**: skip extraction, `advance_session`, and all writes.
- Load committed session N vs N-1 directly, score, pass/block.
- No LLM call, no AST, no writes.

**Constructor** — add `repo_name` and `agent_id`:

```python
def __init__(
    self,
    llm_client: LLMClient,
    store: BaseContractStore,
    drift_threshold: float = DRIFT_THRESHOLD,
    repo_name: str | None = None,    # NEW
    agent_id: str | None = None,     # NEW
) -> None:
```

Drift scoring, pass/block logic, and checkpointing are otherwise unchanged. The cache
path works across backends via the `VectorContractStore.root` alias — note the cache is
largely irrelevant once extraction drops from ~70s to ~500ms, but the alias avoids a
silent no-op.

---

### 7. `memory/app.py` — MODIFY

Wire `SchemaCaptureClient`, store-backend selection, and the managed flag:

```python
store_type = os.environ.get("CDMAD_STORE", "json")

if store_type == "vdb":
    store = VectorContractStore(
        repo_name=os.environ.get("CDMAD_REPO_NAME") or get_repo_name(),
        vdb_root=os.environ.get("CDMAD_VDB_PATH", ".cdmad/vdb"),
    )
else:
    store = ContractStore(root=os.environ.get("CDMAD_ROOT", ".cdmad"))

client = SchemaCaptureClient(
    schema_path=os.environ.get("CDMAD_SCHEMA_PATH", ".cdmad/session_schema.json"),
    repo_name=os.environ.get("CDMAD_REPO_NAME"),
)

gate = MemoryGate(
    llm_client=client,
    store=store,
    repo_name=os.environ.get("CDMAD_REPO_NAME"),
    agent_id=os.environ.get("CDMAD_AGENT_ID"),
)
```

The mode (solo vs managed) is read from `CDMAD_MANAGED` inside the gate. `CDMAD_STORE=json`
(default) keeps current behavior exactly. No existing setup breaks.

---

### 8. `scripts/prime_vdb.py` — NEW

Standalone VDB primer — a thin CLI wrapper around `conductor/vdb_io.py`'s
`prime_vdb_from_codebase` (not a duplicate).

```bash
python3 scripts/prime_vdb.py                          # prime current repo
python3 scripts/prime_vdb.py --repo GolemLinux        # override repo name
python3 scripts/prime_vdb.py --vdb-path /shared/cdmad/vdb
```

Use cases: auditing an existing repo without running agents; resetting a repo's VDB
baseline after a major refactor; keeping the VDB current on every CI merge.

---

### 9. `scripts/migrate_contracts.py` — NEW

One-time migration from the JSON store (`ContractStore`) to the VDB
(`VectorContractStore`). Non-destructive — the JSON archive is preserved until the
migration is confirmed clean.

```bash
python3 scripts/migrate_contracts.py                  # migrate current repo
python3 scripts/migrate_contracts.py --repo ci-wrapper
python3 scripts/migrate_contracts.py --dry-run        # show plan, write nothing
```

**Migration steps:**

```
1. Load all contracts from .cdmad/contracts/{module}/gen_*.json
2. Group by session_id (extracted from generation_id field)
3. Write session JSON files to .cdmad/vdb/{repo_name}/
4. Build index.json — latest and previous session pointers
5. Archive .cdmad/contracts/ → .cdmad/contracts_archive/
6. Write migration receipt → .cdmad/migration_{timestamp}.json
```

Run once per repo. Idempotent after the first run.

---

### 10. Agent Prompt Additions

Two mandatory lines added to the COMMIT PROTOCOL block in every agent prompt:

```
When your files are complete, signal the Conductor:
  touch {staging_dir}/.done

Before resubmitting after a conflict, read carefully:
  {staging_dir}/.conflict_report.txt
Address EVERY point in the report before rewriting.
Do not resubmit until all points are resolved.
```

The Conductor additionally injects `module_key` into each agent's
`session_schema.json` template (see Module-key resolution, §3).

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `CDMAD_STORE` | `json` | `json` or `vdb` — selects contract store backend |
| `CDMAD_VDB_PATH` | `.cdmad/vdb` | Root directory for VDB JSON store |
| `CDMAD_REPO_NAME` | auto from git | Override repo name for scoping |
| `CDMAD_SCHEMA_PATH` | `.cdmad/session_schema.json` | Where the agent writes its declared schema |
| `CDMAD_AGENT_ID` | auto hostname+pid | Identifies the agent's live schema file in VDB |
| `CDMAD_MANAGED` | unset (solo) | `1` when the Conductor launches the gate run — selects read-only managed mode |
| `CDMAD_PEER_CHECK` | `1` | Set `0` to disable intra-session peer checking |
| `CDMAD_RETRIEVAL_TOP_K` | `20` | Max contracts retrieved per commit (future ChromaDB) |
| `CDMAD_LOCAL_DEV` | unset | Pre-existing, in active use — retained for completeness |

**Removed:** `ANTHROPIC_API_KEY` — no longer required by the memory gate. Remove from the
`.env.example` memory-gate section; keep as a comment noting it may be needed if
`AnthropicClient` is re-enabled.

**Unchanged:** all other existing `CDMAD_*` variables retain their current behavior.
`CDMAD_STORE=json` (default) keeps the JSON store active. No existing `.env` files break.

---

## `requirements.txt` Changes

```
# REMOVE — only after memory/llm_client.py makes `import anthropic` lazy
anthropic

# ADD — nothing
# chromadb deferred to enterprise VDB phase
# Current VDB is pure JSON — no new dependencies
```

---

## Tests

`tests/memory/__init__.py` must be created first — the package directory does not exist
yet.

### `tests/memory/test_schema_capture.py` — NEW
```
- schema present → correct ContractSummary produced
- schema missing → AST fallback fires, no error
- schema malformed JSON → clean error, falls back to AST
- schema missing module → falls back to AST for that module only
- module_key present → resolves correct module entry
- module_key absent → falls back to module_name
- .py AST extraction: classes, functions, imports
- .rs regex extraction: pub fn, pub struct, pub trait, use statements
- .rs assumption comments: // must, // requires, // assumes
- _get_repo_name: git remote present → correct name
- _get_repo_name: no git remote → directory name fallback (handles hyphens)
```

### `tests/memory/test_vector_store.py` — NEW
```
- save_contract → load_latest_contract round-trip
- load_previous_contract with before_sequence
- list_contracts ordered by sequence
- repo isolation: ci-wrapper contracts do not appear in GolemLinux
- session isolation: session_abc contracts do not overwrite session_def
- live/ keying: two modules from one agent do not collide
- commit_session: atomic write, index updated, live/ cleared
- write_expected_interfaces → file readable, correct schema
- write_dependency_graph → file readable, correct schema
- retrieve_by_files: returns only contracts for touched files
- retrieve_by_files: empty file list → empty result, no error
- load_latest_contract: index stale → fallback scan works
- root property aliases repo_dir
- VDB directory created on first use if not present
```

### `tests/memory/test_peer_checker.py` — NEW
```
- COLLISION: same symbol exposed by two agents → detected
- COLLISION: different symbols, same agents → no conflict
- INTERFACE_MISMATCH: consumes wrong return type → detected
- INTERFACE_MISMATCH: consumes wrong parameter type → detected
- INTERFACE_MISMATCH: consumes symbol not yet exposed → detected
- ASSUMPTION_CLASH: contradicting no_std assumptions → detected
- clean state: all consumes match all exposes → empty conflict list
- format_conflict_report attempt 1: what + why, no code block
- format_conflict_report attempt 2: + correct signature
- format_conflict_report attempt 3: + code block
- read_all_live: reads all agent_*__*.json from live/ correctly
- read_all_live: empty live/ → empty dict, no error
```

### `tests/memory/test_migration.py` — NEW
```
- JSON → VDB: no contract loss (all modules migrated)
- JSON → VDB: sequence ordering preserved
- index.json correctness: latest and previous pointers correct
- archive created: .cdmad/contracts_archive/ present after migration
- originals preserved: archive contents match pre-migration originals
- idempotent: running twice produces same result
- dry run: no files written, correct report printed
- migration receipt written: .cdmad/migration_{timestamp}.json present
```

### `tests/memory/test_memory_gate.py` — MODIFY
```
- parametrize existing tests over ContractStore and VectorContractStore
- same gate behavior expected from both backends
- no AnthropicClient import anywhere in gate execution path
- SchemaCaptureClient: AST path exercised in gate integration test
- SchemaCaptureClient: schema path exercised in gate integration test
- solo mode: commit_session called once after module loop (VDB backend)
- managed mode (CDMAD_MANAGED=1): read-only, no extraction, no writes
- drift scoring unchanged: same scores from both backends given same contracts
```

---

## Implementation Order

Each step depends only on what precedes it. The conductor package (step 0) may proceed
in parallel with steps 1–6 once its imports (`schema_capture`, `peer_checker`,
`contract_store`) exist. The anthropic removal is hard-gated behind the lazy-import fix.

```
0.  conductor/ package                new — greenfield (~600–900 LOC)
1.  memory/llm_client.py              modify — lazy anthropic import (gate for step 9)
2.  memory/schema_capture.py          new — no deps
3.  memory/contract_store.py          modify — all 9 abstract + 4 new methods
4.  memory/peer_checker.py            new — depends on contract_store
5.  memory/memory_gate.py             modify — solo/managed branch
6.  memory/app.py                     modify — wire client/store + managed flag
7.  scripts/prime_vdb.py              new — wraps conductor/vdb_io.py
8.  scripts/migrate_contracts.py      new — depends on contract_store
9.  requirements.txt                  remove anthropic — ONLY after step 1
10. .env.example                      add new vars, retain CDMAD_LOCAL_DEV
11. tests/memory/                     after all implementation is done
```

**Pre-implementation cleanup (not spec code, do first):** remove the repo-root cruft
left by a brace-expansion that did not expand — a stray file named `=0.40.0` and a file
named `{gates,orchestrator,hooks,tests,scripts}`.

---

## Design Decision — `models.py` and `consumes`

**Decision: `models.py` is not modified.**

The `consumes` field lives in the raw agent schema (`session_schema.json`) and in the
live VDB schemas (`live/{agent_id}__{module}.json`). `PeerChecker` reads these raw dicts
directly and performs `consumes` enforcement there. `ContractSummary.exposes` remains
`list[str]`; no `consumes` field is added.

- **Tier 1 (Conductor / `PeerChecker`)** — owns `consumes` enforcement: symbol- and
  signature-level mismatch. Full `consumes` data available from the raw schemas.
- **Tier 2 (memory gate / `drift_scorer`)** — owns architectural drift scoring against
  prior committed sessions. Compares `contracts`, `assumptions`, `dependencies`. Does
  not see `consumes` or signature-level changes.

This is a known, accepted limitation of the solo CI path (see Two-Tier Drift Detection).

---

## VDB Directory Reference

```
.cdmad/
  vdb/
    {repo_name}/
      index.json                    latest_session, previous_session pointers
      expected_interfaces.json      Conductor baseline (written pre-generation)
      dependency_graph.json         parse order + agent dependency map
      session_{id}.json             committed generation — full contract corpus
      session_{prev_id}.json        prior committed generation
      session_state.json            VDB-backend session state
      checkpoints/
        checkpoint_{session}_{seq}.json
      live/
        {agent_id}__{module}.json   in-progress, keyed by (agent_id, module)
        ...                         cleared on atomic commit
  contracts_archive/                migrated from ContractStore (read-only after migration)
  session_schema.json               agent-declared schema (written per session, read by gate)
  memory_cache.json                 existing cache (unchanged)
  session.json                      existing JSON-backend session state (unchanged)
```

---

## Future: ChromaDB Enterprise Swap

The `VectorContractStore` interface is designed for a clean swap to ChromaDB or Pinecone
when contract count exceeds ~100 or when cross-repo awareness is needed.

The swap is one implementation change — `PersistentClient` instead of JSON file reads —
with no changes to gate logic, drift scorer, orchestrator, or pre-commit hook. The
`CDMAD_RETRIEVAL_TOP_K` env var is already in place for when semantic similarity search
replaces direct file reads; `retrieve_by_files` becomes a vector query.

```python
# Current (this spec)
client = chromadb_stub.JSONClient(path=".cdmad/vdb")

# Future enterprise swap
client = chromadb.PersistentClient(path=".cdmad/chroma")
# or
client = chromadb.HttpClient(host=os.environ["CDMAD_CHROMA_URL"])
```

The gate logic never sees the difference.

---

## CDMAD Principles — Unchanged

1. **Generation is optional. Verification is not.**
2. **Never self-certify.**
3. **Declare assumptions before generating.**
4. **The merge token is the only valid exit condition.**
5. **Constraints are not limitations. They are the architecture.**

The Conductor and atomic commit are infrastructure for Principle 1. The VDB and peer
checker are infrastructure for Principle 3. The merge token still belongs to the
orchestrator.

---

## Ready for Implementation Checklist

- [x] Addendum reviewed and signed off
- [x] Original spec + addendum consolidated into single document
- [ ] Repo root cruft cleaned up (`=0.40.0`, `{gates,orchestrator,hooks,tests,scripts}`)
- [ ] Implementation order followed exactly as specified
- [ ] No code written until consolidation is confirmed and signed off

---

*GateChain + Conductor — Consolidated Spec v2.0*
*Copyright (c) 2026 TrueSystems LLC. All rights reserved.*
*Developed under the CDMAE methodology.*
