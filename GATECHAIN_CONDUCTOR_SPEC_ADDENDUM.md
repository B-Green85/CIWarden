# GateChain + Conductor — Spec Addendum

**Copyright (c) 2026 TrueSystems LLC. All rights reserved.**
**Status:** Authoritative implementation guide — corrects and extends `GATECHAIN_CONDUCTOR_SPEC.md`
**Date:** May 31, 2026

---

## Purpose

This addendum corrects four blocking conflicts and one self-contradiction in the
original spec, and records two previously-open design decisions. Where this
document and `GATECHAIN_CONDUCTOR_SPEC.md` disagree, **this document wins.**

The original spec's two-tier drift model and overall architecture are sound and
carry forward unchanged. Only the plumbing — file map, store surface, import
chain, the gate's read/write role, and the commit path — is corrected here.

The original spec is **not** modified. When this addendum is signed off, both
documents will be consolidated into a single authoritative spec before any code
is written.

---

## 1. Corrected File Map

This table **replaces** the original spec's File Map (original §"File Map",
lines 126–148) in its entirety.

| Path | Original spec | Correct action | Reason |
|---|---|---|---|
| `conductor/` | Not listed | **NEW PACKAGE** — greenfield, ~600–900 LOC | Nothing named conductor exists in the repo (#1) |
| `conductor/__init__.py` | Not listed | **NEW** | Package entry point |
| `conductor/cli.py` | Not listed | **NEW** | Setup wizard — agent count, subsystem paths, prompts, confirm |
| `conductor/session.py` | Not listed | **NEW** | `ConductorSession` dataclass + state management |
| `conductor/vdb_io.py` | Not listed | **NEW** | `load_vdb_index`, `write_session_vdb`, `prime_vdb_from_codebase`, `_get_repo_name` |
| `conductor/distributor.py` | Not listed | **NEW** | Staging dir layout, prompt injection, `.done` polling |
| `conductor/prover.py` | Not listed | **NEW** | Parse staging in dependency order, drive `PeerChecker`, escalate reports |
| `conductor/commit.py` | Not listed | **NEW** | Atomic commit: `commit_session` → copy to worktree → enqueue ONE queue entry |
| `memory/llm_client.py` | Not listed | **MODIFY** — make `import anthropic` lazy | Top-level import breaks when package removed (#3) |
| `memory/schema_capture.py` | NEW | NEW | Unchanged |
| `memory/contract_store.py` | MODIFY | **MODIFY** — implement all 9 abstract + 4 new methods | Spec miscounted; `list_contracts` omitted (#2) |
| `memory/peer_checker.py` | NEW | NEW | Unchanged |
| `memory/memory_gate.py` | MODIFY — "2 changes only" | **MODIFY** — add solo/managed mode branch | "`_execute` unchanged" is false on VDB path (#4) |
| `memory/app.py` | MODIFY | **MODIFY** — also pass `managed` flag | Follows #4 |
| `memory/models.py` | Zero changes | **EXPLICIT DECISION: no changes** | See §6 — Tier 1 owns `consumes` enforcement |
| `scripts/prime_vdb.py` | NEW | **NEW** — thin CLI wrapper around `conductor/vdb_io.py` | Shared primer lives in conductor package |
| `scripts/migrate_contracts.py` | NEW | NEW | Unchanged |
| `tests/memory/__init__.py` | Not listed | **NEW** | Package dir does not exist yet |
| `tests/memory/test_*.py` | NEW/MODIFY | Unchanged | — |
| `requirements.txt` | Remove anthropic | **MODIFY** — only after #3 lands | Sequential dependency |
| `.env.example` | MODIFY | **MODIFY** — add new vars, retain `CDMAD_LOCAL_DEV` | `CDMAD_LOCAL_DEV` omitted from original table |

**Zero changes to:** `drift_scorer.py`, `models.py`, `orchestrator/`, pre-commit hook,
other gates (lint, typecheck, security, test, stress, build), `queue/commit_queue.py`.

---

## 2. Corrected VDB Directory Layout

This **replaces** the original spec's storage layout (original §2, lines 272–286)
and VDB Directory Reference (lines 779–798). The only change is `live/` keying —
see Blocker #4.

```
.cdmad/
  vdb/
    {repo_name}/
      index.json                       latest_session, previous_session pointers
      expected_interfaces.json         Conductor baseline (written pre-generation)
      dependency_graph.json            parse order + agent dependency map
      session_{id}.json                committed generation — full contract corpus
      session_{prev_id}.json           prior committed generation
      session_state.json               VDB-backend session state
      checkpoints/
        checkpoint_{session}_{seq}.json
      live/
        {agent_id}__{module}.json       in-progress, keyed by (agent_id, module)
        {agent_id}__{module}.json       cleared on atomic commit
        ...
  contracts_archive/                   migrated from ContractStore (read-only after migration)
  session_schema.json                  agent-declared schema (written per session, read by gate)
  memory_cache.json                    existing cache (unchanged)
  session.json                         existing JSON-backend session state (unchanged)
```

**Keying change:** the original spec keyed live files as `live/agent_001.json`
(agent only). Because the gate's `_execute` loops over modules, an agent-only key
causes every module to overwrite the same file. Live schemas are keyed by
**(agent_id, module)** — `live/{agent_id}__{module}.json`. See Blocker #4.

---

## 3. Corrected Session Flow

This **replaces** the atomic-commit portion of the original spec's Session Flow
(original §"Session Flow", lines 95–108). The proofing and distribution phases
above it are unchanged.

```
[ALL AGENTS CLEAN — peer checker reports no conflicts]
    │
    ▼
commit_session() writes the VDB session file atomically
  session_{id}.json written + index.json updated + live/ cleared
    │
    ▼
Copy all staged files from /tmp/conductor_staging/ → worktree
    │
    ▼
Enqueue ONE queue entry (full file list, single message)
    │
    ▼
Queue worker: git add -- <files>  +  git commit --no-verify
    │
    ▼
[GATE CHAIN RUNS — existing orchestrator behavior]
  lint → typecheck → security → memory → test → stress → build
  Memory gate runs in MANAGED mode — reads committed VDB (session N vs N-1)
    │
    ▼
All gates pass → merge token issued (orchestrator)
```

**Critical ordering:** `commit_session()` MUST complete before the gate chain
runs. The managed-mode memory gate reads the committed VDB; if the VDB write
slips after the gate run, the gate reads stale state and inter-session drift
scoring is wrong. See Blocker #8.

---

## 4. Blocker #1 — Conductor is greenfield, not 6 edits

The original spec (§6, line 134; Implementation Order step 6; File Map) labels
`conductor.py` as **MODIFY (6 additions)** and references existing symbols
(`send_conflict_report`, `ConductorSession`, `load_vdb_index`,
`write_session_vdb`). **No conductor file exists anywhere in the repo** — the
only occurrence of the word is in the spec itself.

The conductor is a **new package** of approximately **600–900 lines** split across
seven files (see corrected file map). The original spec's "6 additions" are
features that live inside this package, not edits to an existing file:

| Original "addition" | Lands in |
|---|---|
| Startup VDB prime | `conductor/vdb_io.py` (`prime_vdb_from_codebase`) |
| Dependency graph extraction | `conductor/prover.py` |
| Expected interfaces pre-validation | `conductor/prover.py` |
| `consumes` field in agent schema | `conductor/prover.py` + `conductor/distributor.py` |
| Escalating conflict reports | `conductor/prover.py` |
| `.done` / `.conflict_report.txt` protocol | `conductor/distributor.py` |

**Scope statement:** this is ~600–900 LOC of new code, not edits to an existing
file. Effort and review must be planned accordingly.

---

## 5. Blocker #2 — Complete the `VectorContractStore` surface

`BaseContractStore` declares **9** abstract methods
(`contract_store.py:24–71`), not 5 as the original spec states (§2, line 305).
A subclass that does not implement all 9 remains abstract and raises `TypeError`
at construction. The original spec also omits `list_contracts` entirely.

**All 9 abstract methods must be implemented:**

```
save_contract            load_latest_contract     load_previous_contract
list_contracts           save_checkpoint          load_latest_checkpoint
get_or_create_session    save_session             advance_session
```

**Plus 4 new methods** (the original spec says "three new" but lists four —
use **4**):

```
commit_session           retrieve_by_files        write_expected_interfaces
write_dependency_graph
```

**Also port the non-abstract helpers the gate path touches:**

- `load_session` — used by `get_or_create_session` and the cache path.
- a VDB equivalent of `list_modules` — used implicitly during module discovery.

`commit_session` is the only method permitted to write the committed session
file and update `index.json`. In a Conductor run it is called once, by the
Conductor, after all agents are clean. In the solo CI path it is called once by
the gate (see Blocker #4). It is never called per-module mid-loop.

---

## 6. Blocker #3 — Fix the import chain before removing anthropic

`memory/llm_client.py:10` has an unconditional top-level `import anthropic`.
That module is imported transitively by `memory_gate.py`, `app.py`, and the new
`schema_capture.py` (which must `from memory.llm_client import LLMClient`).
Removing the `anthropic` package without fixing this import produces a
`ModuleNotFoundError` that breaks the entire memory gate at load time.

**Fix — make the import lazy inside `AnthropicClient`:**

```python
class AnthropicClient(LLMClient):
    def __init__(self, api_key=None, model="claude-haiku-4-5-20251001"):
        import anthropic  # lazy — only when AnthropicClient is actually used
        self._anthropic = anthropic
        ...
```

This preserves `AnthropicClient` as the commented re-enable path the original
spec wants. `httpx` remains a top-level import and stays in `requirements.txt`.

**Sequencing:** `llm_client.py` is added to the file map as **MODIFY**. Removing
`anthropic` from `requirements.txt` is safe **only after** this fix lands. This
is a hard ordering constraint — see the implementation order.

---

## 7. Blocker #4 — The reader/writer split (`_execute` cannot be unchanged)

The gate today is the **writer**: `_execute` (`memory_gate.py:146–186`) does
`get_or_create_session` → `advance_session` → per-module `extract_contracts` →
`save_contract` → `load_previous_contract` → `save_checkpoint`. The new model
makes the **Conductor** the writer. These cannot coexist with `_execute`
untouched (original §4, line 465 claims they can).

On `VectorContractStore`, an unchanged `_execute` breaks two ways:
1. `save_contract` writes to `live/` as *pending*; nothing reaches a committed
   session except via `commit_session`. On the normal single-commit CI path
   (no Conductor), `load_latest`/`load_previous` read `index.json → session file`
   and never see the gate's own writes — inter-session drift silently dies.
2. The per-module loop calls `save_contract` once per module against a single
   live file — every module overwrites the last (fixed by the keying change below).

**Resolution — explicit mode branch in `_execute`, selected by `CDMAD_MANAGED`:**

**Solo mode** (default CI path, no Conductor, `CDMAD_MANAGED` unset):
- Gate extracts via `SchemaCaptureClient`.
- Buffers all module summaries across the module loop (it already builds
  `all_summaries`).
- After the loop, calls `commit_session(all_summaries)` **once** for the VDB
  backend — not per-module `save_contract`.
- Then calls `load_previous_contract` to read committed N−1 and scores.
- JSON backend keeps existing per-module `save_contract` behavior, unchanged.

**Managed mode** (`CDMAD_MANAGED=1`, Conductor session active):
- The Conductor already wrote and committed the VDB during the proofing phase.
- Gate is **read-only**: skip extraction, `advance_session`, and all writes.
- Load committed session N vs N−1 directly, score, pass/block.
- No LLM call, no AST, no writes.

`CDMAD_MANAGED` is added to the environment variables table (§9).

### 7.1 `live/` keying fix (part of #4)

Key live schema files by **(agent_id, module)**, not agent alone:

```
.cdmad/vdb/{repo}/live/{agent_id}__{module}.json
```

The corrected VDB layout (§2) reflects this. Without it the gate's module loop
and the Conductor's per-agent multi-module output both collapse to one file.

### 7.2 Cache alias fix (part of #4)

`_cache_path` does `getattr(self.store, "root", None)` (`memory_gate.py:95`).
`VectorContractStore` exposes `repo_dir`, not `root`, so the cache silently
no-ops on the VDB backend. Add a `root` property on `VectorContractStore` that
returns `repo_dir`.

The cache is **largely irrelevant** once extraction drops from ~70s (Haiku) to
~500ms (AST/schema) — but the alias keeps behavior consistent across backends
and avoids a silent no-op that would confuse future debugging.

---

## 8. Blocker #8 — One atomic commit, not "one per file"

The original spec contradicts itself: "issue an atomic commit" (line 15, 38) vs
"Enqueue files via commit queue (one per file)" (line 99). These are
incompatible — N files one-per-entry is N commits, not one atomic commit.

**Resolution — the Conductor enqueues ONE queue entry with the full file list.**
The queue already supports this: `enqueue()` accepts a file list
(`queue/commit_queue.py:118`) and the worker does `git add -- <files>` followed
by a single `git commit` (`queue/commit_queue.py:289–298`).

**Rules:**
- The Conductor does **not** call `git commit` itself.
- The Conductor copies staged files into the worktree, then hands the commit to
  the queue worker via one `enqueue` call with the full file list.
- The queue worker commits with `--no-verify` and drives the orchestrator
  (`queue/commit_queue.py:298, 312`), so the gate chain runs exactly once.
- The memory gate runs once, in managed mode, against the full commit.

**Ordering is critical** and is captured in the corrected Session Flow (§3):
`commit_session()` (VDB write) MUST precede the gate run. If the ordering slips,
the managed-mode memory gate reads stale VDB state and inter-session drift
scoring is wrong.

---

## 9. Environment Variables — Additions

This table is **additive** to the original spec's Environment Variables table
(original lines 651–667). All original variables retain their behavior.

| Variable | Default | Description |
|---|---|---|
| `CDMAD_MANAGED` | unset (solo) | `1` when the Conductor launches the gate run — selects read-only managed mode (§7) |
| `CDMAD_LOCAL_DEV` | unset | Pre-existing, in active use — was omitted from the original spec table; documented here for completeness |

`CDMAD_STORE=json` (default) continues to keep the current JSON store active.
No existing `.env` files break.

---

## 10. Design Decision — `models.py` and `consumes` (issue #5)

**Decision: `models.py` is NOT modified.**

The `consumes` field lives in the raw agent schema (`session_schema.json`) and in
the live VDB schemas (`live/{agent_id}__{module}.json`). `PeerChecker` reads
these raw dicts directly and performs `consumes` enforcement there.
`ContractSummary.exposes` remains `list[str]`; no `consumes` field is added to
`ContractSummary`.

Tier boundary, stated explicitly:

- **Tier 1 (Conductor / `PeerChecker`)** — owns `consumes` enforcement:
  symbol-level collisions and signature-level mismatches. Full `consumes` data
  available from the raw schemas.
- **Tier 2 (memory gate / `drift_scorer`)** — owns architectural drift scoring
  against prior committed sessions. Compares `contracts`, `assumptions`,
  `dependencies` (`drift_scorer.py:26–62`). Does **not** see `consumes` or
  signature-level changes.

> **Known and accepted limitation.** Tier 2 does not enforce `consumes` matching.
> The spec's guarantee that "every `consumes` is matched against the corresponding
> `exposes`" is enforced entirely by Tier 1 (the Conductor and `PeerChecker`). An
> agent that runs the solo CI path (no Conductor) has its architectural contracts
> scored against the prior session, but its `consumes` declarations are not
> cross-validated. This is an accepted limitation of the solo CI path.

---

## 11. Design Decision — Module-name keying for foreign repos (issue #6)

**Problem.** The memory gate discovers modules by top-level directory
(`memory_gate.py:37` → `gates`, `orchestrator`, `memory`, …). A foreign target
such as GolemLinux (a Rust kernel) uses subsystem directories
(`src/boot/`, `src/memory/`, `src/scheduler/`, …). These namespaces do not align,
so `_from_schema(module_name=…)` lookups miss when the gate's discovered name
differs from the schema's module key.

**Resolution.** The Conductor stamps each agent's schema with an explicit
`module_key` when it distributes prompts. That key — not the discovered directory
name — is what the gate uses for VDB lookups.

Injected into every agent's `session_schema.json` template:

```json
{
  "module_key": "scheduler",
  "repo": "GolemLinux",
  "session_id": "...",
  "modules": { "...": {} }
}
```

`SchemaCaptureClient._from_schema()` reads `module_key` when present, falling back
to the discovered directory name when absent (the solo CI path on Python repos
where the names already align).

`module_key` is added to:
- the agent schema format section (on consolidation),
- the Conductor's prompt distribution logic (`conductor/distributor.py`),
- `SchemaCaptureClient._from_schema()` lookup logic.

---

## 12. Smaller Issues — Documented, Not Blocking

- **`tests/memory/` does not exist.** `tests/memory/__init__.py` must be created
  (added to the file map). Tests mirror module structure per `CLAUDE.md`.
- **`.env.example` has only 3 lines today.** The new `CDMAD_*` vars are additive.
  `CDMAD_LOCAL_DEV` is in active use and must be retained — it was omitted from
  the original spec's environment table (see §9).
- **`_get_repo_name()` is referenced but never defined** in the original spec
  (lines 186, 696). Define it canonically in `conductor/vdb_io.py` and import it
  from there in `schema_capture.py`. It must handle hyphens in repo/directory
  names (the `ci-wrapper` hyphen is a known mypy issue per project memory).
- **Repo-root cruft (not spec-related; clean before implementation):** a stray
  file named `=0.40.0` and a file named `{gates,orchestrator,hooks,tests,scripts}`
  left by a brace-expansion that did not expand. Remove both before starting.

---

## 13. Implementation Order

Unchanged from the original spec (lines 759–775) except: **step 0 is the
conductor package build**, and **the anthropic removal is hard-gated behind the
`llm_client.py` lazy-import fix**.

```
0.  conductor/ package                new — greenfield (may proceed in parallel
                                       with steps 1–5; depends on schema_capture
                                       + peer_checker + contract_store for imports)
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

---

## 14. Implementation Deviations (recorded during build, 2026-05-31)

Deviations from this signed-off addendum that surfaced during implementation,
recorded here for the decision trail.

### 14.1 `_get_repo_name` lives in `memory/schema_capture.py`, not `conductor/vdb_io.py`

**Addendum said** (§1 file map, §6, §12): define `_get_repo_name` canonically in
`conductor/vdb_io.py` and import it from there into `memory/schema_capture.py`.

**Implemented instead:** `_get_repo_name` is defined in `memory/schema_capture.py`
(the lower layer) and **re-exported** from `conductor/vdb_io.py` (via
`from memory.schema_capture import ... _get_repo_name` + `__all__`). Callers may
still import it from either module.

**Why:** the addendum's direction creates a circular import and inverts the layering.
`conductor/vdb_io.py` imports `SchemaCaptureClient` from `memory/` (it calls it during
`prime_vdb_from_codebase`). If `schema_capture.py` also imported `_get_repo_name` from
`conductor/vdb_io.py`, the two modules would import each other at load time. It also
contradicts the original spec's own rule (original §1: "`conductor.py` imports from
[schema_capture], not the other way around") — `memory/` must not depend on
`conductor/`. Defining the helper in the lower layer and re-exporting upward keeps a
single source of truth, the correct dependency direction (conductor → memory, never
the reverse), and no cycle.

**Impact:** none on behavior. Same function, same signature, same hyphen handling
(verified: `ci-wrapper`/`ci-gate-wrapper` resolve correctly). Only the canonical home
moved one layer down.

---

## Ready for Implementation Checklist

- [ ] Addendum reviewed and signed off
- [ ] Original spec + addendum consolidated into single document
- [ ] Repo root cruft cleaned up (`=0.40.0`, `{gates,orchestrator,hooks,tests,scripts}`)
- [ ] Implementation order followed exactly as specified
- [ ] No code written until consolidation is complete and signed off

---

*GateChain + Conductor Update Spec — Addendum*
*Copyright (c) 2026 TrueSystems LLC. All rights reserved.*
*Developed under the CDMAE methodology.*
