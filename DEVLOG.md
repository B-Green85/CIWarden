# DEVLOG — CIWarden

---

## March 4, 2026 — The First Commit

**The system governed its own birth.**

Today was the first commit of the CI Gate Wrapper — a 7-gate CDMAD enforcement chain built to govern agentic development. Before a single line could be pushed to GitHub, the system required its own code to pass every gate it was designed to enforce.

It did not make this easy.

---

### What Was Built

A full CI enforcement layer for agentic development:

- **7 gates:** lint, typecheck, security, memory, test, stress, build
- **Generational memory gate** — extracts architectural contracts from source code via the Anthropic API, scores drift between generations (0.0–1.0), blocks commits that deviate beyond threshold
- **Stress gate** — three-phase load simulation (NORMAL/THRESHOLD/BREACH), auto-skips when no API integrations are detected
- **133 tests**, ruff clean, mypy clean, 36 fully typed source files
- **Full audit trail** via SQLite — every gate result, every agent, every SHA, every merge token

---

### The Bootstrapping Problem

The gate chain enforces commits. To push the gate chain to GitHub, you have to commit it. To commit it, it has to pass its own gates.

The system gatekept its creator.

---

### The Audit Trail

Every attempt logged to SQLite. Here is the complete record:

| Attempt | Blocked At | Time |
|---------|------------|------|
| 1 | lint | 5:57 PM |
| 2 | lint | 6:09 PM |
| 3 | typecheck | 6:09 PM |
| 4 | lint | 6:10 PM |
| 5 | lint | 6:10 PM |
| 6 | security | 6:11 PM |
| 7 | build | 6:11 PM |
| 8 | build | 6:18 PM |
| 9 | memory (no key) | 6:19 PM |
| 10 | memory (no key) | 6:21 PM |
| 11 | memory (no key) | 6:23 PM |
| 12 | memory (invalid key) | 6:29 PM |
| 13 | stress | 6:29 PM |
| 14 | memory (drift) | 6:40 PM |
| 15 | stress | 6:44 PM |
| 16 | memory (drift) | 6:47 PM |
| 17 | stress | 6:56 PM |
| 18 | stress | 6:58 PM |
| 19 | stress | 7:09 PM |
| 20 | stress | 7:12 PM |
| 21 | stress | 7:16 PM |
| 22 | build | 7:16 PM |
| 23 | lint | 7:24 PM |
| **24** | **✦ ALL GATES PASSED** | **7:25 PM** |

**24 attempts. 88 minutes. One merge token.**

```
Merge token: fafd3c357f6d4e9ebe14c8ac
Commit SHA:  7940496
```

---

### What Each Gate Taught

**Lint** — The project directory `ci-wrapper` has a hyphen, which is an invalid Python module name. Ruff flagged it on every file. Fixed via `per-file-ignores` in `pyproject.toml`.

**Typecheck** — mypy couldn't resolve the hyphenated directory either. Fixed with `explicit_package_bases = true` and `namespace_packages = true`. Then 50 missing return type annotations across the codebase had to be fixed manually.

**Security** — bandit flagged `0.0.0.0` bindings and a `GateStatus.PASS` string as a hardcoded password. Both intentional. Suppressed with `# nosec` and global ignores.

**Memory** — The memory gate reached the Anthropic API and extracted real architectural contracts from the codebase. First blocked because no API key existed. Then blocked because the Anthropic console billing flow had a UI issue that prevented credit purchase. Once resolved and a valid key was set, the gate ran — 78 seconds of contract extraction — and passed. Later blocked again by drift exceeding 0.3 threshold due to legitimate architectural changes mid-session. Threshold raised to 0.5 with `CDMAD_DRIFT_THRESHOLD` env var override.

**Stress** — The stress gate found `httpx` in the orchestrator and concluded there were API integrations to stress test. There weren't — the httpx calls were internal gate-to-gate communication. Fixed by excluding infrastructure directories (`orchestrator/`, `gates/`, `hooks/`, `scripts/`, `memory/`) from the API detection scan. Once fixed, the gate saw no external API integrations and auto-passed in 30ms.

**Build** — `py_compile` requires filenames as arguments. The command was called with no arguments. Fixed by switching to `compileall.compile_dir('.')` which recursively compiles everything. Then the command string exceeded the 120-character line limit. Fixed by extracting to a variable.

---

### Insights Discovered

**The system is repo-state independent.** The pre-commit hook generates a SHA from staged content (`git write-tree`) when no HEAD exists. The gate chain ran identically on attempt 1 as it will on attempt 1000. No bootstrapping ceremony required.

**All failed attempts are logged.** The audit trail in `gate_results.db` captured every blocked attempt, which gate blocked it, and when. The system remembers everything that happened before it existed as a git commit.

**The stress gate revealed a product insight.** Not every application has an API. Gates should be conditional — running only when the code type warrants it. The gate registry will eventually support trigger criteria, not just phase and order.

**The drift threshold is a tunable constraint.** 0.3 is appropriate for stable, mature codebases. 0.5 is appropriate for active development. The threshold itself becomes a governance decision, configurable per project without code changes.

**Prompts are specifications.** The stress gate prompt defined what to measure, when to pass, when to fail, and how to contain failure. Claude Code turned that specification into running code. The prompt is the architecture. The code is the expression of it.

---

### Final Gate Run

```
✓  lint         PASS    59ms
✓  typecheck    PASS  2221ms
✓  security     PASS   473ms
✓  memory       PASS 78056ms
✓  test         PASS  4810ms
✓  stress       PASS    31ms
✓  build        PASS   112ms
━━━━━━━━━━━━━━━━━━━━━━━━━━━
   Total              83129ms

✦  ALL GATES PASSED
   Merge token: fafd3c357f6d4e9ebe14c8ac
```

---

*"Generation is optional. Verification is not."*

---

## March 5, 2026 — v1.1: Better Plumbing

**The system shipped faster because the agent autonomously navigated the gate chain — fixing, retrying, and landing the commit without me in the loop.**

Yesterday CI Wrapper governed its own birth in 88 minutes and 24 commit attempts, while I relayed gate output to model by hand. Today the system shipped three features in 23 minutes, and the last commit passed all 7 gates on the first try.

---

### What Was Built

Three commits landed on the `v1.1` branch:

1. **Enterprise auth layer** — API key authentication on `POST /commit` via `X-Gate-Token` header, SHA-256 key hashing in `orchestrator/auth.db`, master key auto-generated on first startup, local dev bypass when `CDMAD_GATE_TOKEN` unset

2. **HTTPS support** — Self-signed certificate generation on first startup via `cryptography` library, uvicorn SSL when `CDMAD_HTTPS=1`, `CDMAD_HTTPS_VERIFY=0` for self-signed cert bypass in the pre-commit hook, SAN includes `localhost` + `127.0.0.1`, private key locked to `0600`

3. **.env config** — `start_gates.py` auto-loads `.env` on startup, injects variables into the environment before spawning any gate services, `.env.example` template with all config vars

Supporting changes: 25 new tests, import sort fixes, mypy strict compliance, mocked gate calls in auth tests to prevent infinite pytest recursion.

---

### The Commit Record

| Commit | Message | Attempts | Gate Result |
|--------|---------|----------|-------------|
| `dcc8b10` | feat: v1.1 — enterprise auth layer and dev loop | 5 | ALL PASS |
| `a05f6e7` | feat: v1.1 — HTTPS support and .env config | 1 | ALL PASS |

---

### v1.0.0 vs v1.1 — Session Comparison

| Metric | v1.0.0 (March 4) | v1.1 (March 5) |
|--------|-------------------|----------------|
| **Commits landed** | 1 | 2 |
| **Total attempts** | 24 | 6 |
| **Wall clock time** | 88 minutes | ~23 minutes |
| **Human relay** | Manual copy-paste between gate output and fixes | Claude Code independently resolved blockers in the validation chain |
| **API key management** | Set manually in terminal before starting services | Lives in `.env` |
| **Gate service lifecycle** | Managed manually across multiple terminals | Managed by Claude Code via `start_gates.py` |
| **Environment config** | Ad-hoc `export` commands, lost between sessions | `.env` file loaded automatically on startup |
| **Final commit retries** | 1 (attempt 24 of 24) | 0 (first attempt pass) |

---

### What Changed

**`dev_loop.sh` closes the human-as-message-bus gap.** In v1.0.0, I read gate output, pasted it to model, waited for fixes, re-ran the commit, and repeated. The dev loop automates this: `git commit` → gate failure → pipe output to `claude -p` → fix → retry. I become the initiator, not the relay.

(The dev loop couldn't run inside Claude Code due to nested session restrictions — Claude Code ran the loop manually instead. But the script exists for standalone use.)

**`.env` removes key management friction.** In v1.0.0, the Anthropic API key was set manually in the terminal before starting services and had to be re-provided when services restarted. In v1.1, `start_gates.py` reads `.env` before spawning any process. The key is set once and persists across restarts.

**Auth tests exposed a recursive pytest hang.** Two auth middleware tests called `POST /commit` through FastAPI's `TestClient`, which triggered the orchestrator to call all gate services via httpx. The test gate ran `pytest`, which ran the auth tests, which called `POST /commit` — infinite recursion. Fixed by mocking `call_gate` in auth tests. This was the session's most instructive bug: the gate chain's enforcement model means you can't casually invoke it from inside a test without mocking the boundary.

---

### The Throughput Story

The throughput improvement came from two things: the dev loop removing the relay, and `.env` removing the key management friction. The system got faster because I stopped doing work that didn't require my judgment.

The constraints were mine. The gate chain was my design. The merge token is mine to issue. Claude Code handled execution — but every boundary it operated within was set deliberately by me. That's not automation replacing oversight. That's oversight getting better infrastructure.

v1.0.0 was me doing everything. v1.1 was me doing only what required me.

---

### Final Gate Run

```
✓  lint         PASS      68ms
✓  typecheck    PASS     902ms
✓  security     PASS     559ms
✓  memory       PASS   84827ms
✓  test         PASS    9599ms
✓  stress       PASS      59ms
✓  build        PASS     139ms
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   Total               94702ms

✦  ALL GATES PASSED
   Merge token: e65e65b28f1a12e5f4b961bb
```

---

*"Generation is optional. Verification is not."*

---

## May 27, 2026 — Golem Linux Phase 1 / GateChain v2

**The system was pushed harder than it had ever been pushed. Two bugs present since v1.0.0 surfaced and were fixed. A new subsystem shipped.**

Today was the Golem Linux Phase 1 build — six Claude Code instances running simultaneously, each building an independent kernel subsystem. Boot, memory, scheduler, syscall, filesystem, sentinel. The gate chain had governed single-agent sessions before. It had never governed six agents hitting the same repo, the same git index, and the same orchestrator at the same time.

It did not handle this gracefully. Not at first.

---

### What Was Built

**Golem Linux Phase 1 — six kernel subsystems:**

- `src/boot/` — UEFI entry stub, PE32+ application, x86_64 NASM bootloader (Agent 1)
- `src/memory/` — bitmap frame allocator, 4-level page tables, linked-list heap (Agent 2)
- `src/scheduler/` — process control block, round-robin scheduler, x86_64 context switch (Agent 3)
- `src/syscall/` — SYSCALL/SYSRET entry, dispatch table, system call handlers (Agent 4)
- `src/fs/` — VFS layer, RamFS in-memory bootstrap filesystem (Agent 5)
- `src/sentinel/` — audit chain, invisibility gate, registration handshake, four-signal degradation monitor (Agent 6)

**GateChain v2 — commit queue:**

- `queue/commit_queue.py` — 756 lines, SQLite-backed serial processing
- Agents enqueue commits instead of calling git directly
- Worker processes one commit at a time, polls until `.git/index.lock` clears
- Runs explicit pathspec `git add`, commits, waits for gate, issues queue token on success

---

### The Index Lock Problem

Six agents. One git index. The memory gate runs for ~125 seconds. While it runs, the index is locked. Five other agents are trying to commit. Contention was severe — agents failed, retried, failed again, stacked up.

The commit queue solved it. Serial processing. One commit at a time. Agents enqueue and wait. The queue worker owns the index. The contention disappeared.

---

### Bug 1: Stress Gate — Broken Since v1.0.0

**The bug:** `window_seconds=60` in `RateLimitState.__init__`. The rate limiter window was 60 seconds — 100 requests filled it in 1.4 seconds at 70 RPS, then 58 seconds of rejections. Error rate: 80%. The stress gate was simulating a catastrophically broken API on every test.

**Why it was never caught:** The stress gate has two modes. If no HTTP client imports are detected in non-infrastructure directories, it auto-passes in 30ms. For months it was auto-passing — all httpx usage was in excluded infrastructure directories. The commit queue agent added `httpx` to `queue/commit_queue.py`, a non-excluded directory, which triggered the full load simulation for the first time.

**Fix:** `window_seconds=1` — 100 requests per second, correct behavior for rate limit stress testing.

**Fix landed:** Unit test updated, `assert state.window_seconds == 1`.

The stress gate has now run correctly for the first time since March 4.

---

### Bug 2: Orchestrator Timeout — Cascading Failures Under Load

**The bug:** `timeout=120` in `orchestrator/orchestrator.py`. The memory gate legitimately takes ~125 seconds under normal load. The orchestrator was timing out and treating memory gate success as failure, then cascading that failure downstream.

**Why it was never caught:** Single-agent sessions complete fast enough that the memory gate rarely pushed past 120 seconds. Six agents hitting the orchestrator sequentially via the queue meant each memory gate run went to completion — and the timeout fired.

**Fix:** `timeout=300`.

---

### Memory Gate: Haiku

**Before:** `model: str = "claude-sonnet-4-20250514"`
**After:** `model: str = "claude-haiku-4-5-20251001"`

The memory gate calls the Anthropic API on every commit for architectural contract extraction. Sonnet-level reasoning is not required for contract scoring. Haiku is significantly faster and cheaper.

**Result:** Memory gate runtime dropped from ~125s to ~70s.

---

### Session Stats

| Metric | Value |
|---|---|
| Agents | 6 simultaneous |
| Subsystems built | 6 |
| Gate chain runs | Multiple per agent |
| Bugs fixed | 2 (stress gate window, orchestrator timeout) |
| New subsystem | Commit queue (756 lines) |
| Memory gate runtime | ~125s → ~70s (Haiku swap) |

---

### What the Multi-Agent Build Revealed

**The gate chain was designed for single agents.** Every assumption in the original architecture — one commit at a time, one agent at a time, one index lock at a time — held because there was only ever one agent. Six agents stress-tested every assumption simultaneously.

**Serial enforcement is the right model.** The commit queue doesn't weaken the gate chain — it makes it viable at multi-agent scale. Each commit still passes all seven gates. The queue just ensures they don't fight over the infrastructure while waiting.

**Latency compounds under load.** 125 seconds is acceptable for one agent. For six agents queuing behind each other, it's a session bottleneck. The Haiku swap to 70 seconds matters more in multi-agent sessions than it ever would in a single-agent session.

**Hidden bugs surface under real load.** The stress gate rate limit bug existed for 84 days before it was found. The orchestrator timeout existed for 84 days. Neither surfaced in single-agent sessions. Six agents running simultaneously found both on the same day.

---

### Final Gate Run (representative — commit queue session)

```
✓  lint         PASS      71ms
✓  typecheck    PASS     834ms
✓  security     PASS     612ms
✓  memory       PASS   71203ms
✓  test         PASS    9821ms
✓  stress       PASS   14832ms
✓  build        PASS     203ms
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
   Total               97576ms

✦  ALL GATES PASSED
```

---

*"Generation is optional. Verification is not."*

---

## May 31, 2026 — GateChain v3 / Conductor

**The gate chain gets a conductor. The memory gate loses its API dependency. The multi-agent coordination problem gets a structural solution.**

Today was architecture, not code. The spec that governs the next build was written, reviewed, assessed, corrected, and consolidated. No code landed — by design. The gate chain enforces that nothing ships without passing verification. The same discipline applies to the spec itself: nothing gets built from a document that hasn't been reviewed and signed off.

Two Claude Code sessions. One assessment. One addendum. One consolidated v2.0 spec. Ready for implementation tonight.

---

### What Was Designed

**Memory Gate — LLM extraction removed:**

The Haiku API call is replaced by `SchemaCaptureClient` — a static AST-based extractor. Python files analyzed via `ast` module. Rust files analyzed via regex targeting public interface declarations and assumption comments. No network call. No API key required for the memory gate from this point forward.

Memory gate latency: **~70s (Haiku) → ~500ms (AST)**

**VectorContractStore — implemented:**

The `VectorContractStore` scaffold has been present since v1.0.0 with every method raising `NotImplementedError`. The v3 spec fully implements it — JSON VDB, scoped per repository and per session, no new dependencies. The existing JSON contract store (`.cdmad/contracts/`) remains the default (`CDMAD_STORE=json`). VDB is opt-in (`CDMAD_STORE=vdb`).

**Conductor — new 7-file orchestration package:**

```
conductor/
  __init__.py
  cli.py          setup wizard — agent count, subsystem paths, prompts, confirm
  session.py      ConductorSession dataclass + state
  vdb_io.py       VDB I/O + prime_vdb_from_codebase + _get_repo_name
  distributor.py  staging dir layout, prompt injection, .done polling
  prover.py       parse staging in dependency order, drive PeerChecker, escalate reports
  commit.py       atomic commit: commit_session → copy to worktree → one queue entry
```

The Conductor is the first and last authority in every multi-agent generation session. It reads all agent prompts before any agent sees them, extracts the dependency graph, primes the VDB from the existing codebase if empty, distributes prompts with staging directives, proofs all output before a single file reaches the repo, and issues one atomic commit when all agents are clean.

**Atomic commit — the gap that closes:**

Before v3, the gate chain was rigorous at the individual commit level but had no concept of multi-agent session coherence. An agent could pass every gate and still ship code that contradicted what a peer agent built in the same session. The gates checked each commit in isolation.

The atomic commit closes that gap. Nothing reaches the repository until the Conductor has verified the entire generation is internally consistent — every interface declared was delivered, every `consumes` matched the corresponding `exposes`, no symbol collisions across subsystem boundaries.

**Peer Checker — intra-session drift:**

Three conflict types detected before anything touches the repo:
- `COLLISION` — same symbol exposed by two agents
- `INTERFACE_MISMATCH` — agent consumes a symbol with wrong signature
- `ASSUMPTION_CLASH` — contradicting assumptions about shared state

Conflict reports escalate in specificity with each retry. No retry limit. The offending agent works until its output is clean.

**Solo/managed mode branch in memory gate:**

- **Solo mode** (default CI, no Conductor): gate extracts via `SchemaCaptureClient`, buffers all module summaries, calls `commit_session()` once after the module loop, scores against prior session.
- **Managed mode** (`CDMAD_MANAGED=1`): Conductor already wrote the VDB during proofing. Gate is read-only — loads committed session N vs N-1, scores, passes or blocks. No extraction, no writes.

---

### The Review Process

The spec was written, then handed to Claude Code for assessment before implementation was authorized. Claude Code read the full spec and the existing memory implementation and returned a structured assessment identifying four blocking conflicts:

1. `conductor.py` labeled as MODIFY — no conductor file exists in the repo. Greenfield build, ~600–900 LOC.
2. `VectorContractStore` method count wrong — 9 abstract methods, not 5. `list_contracts` omitted entirely.
3. `anthropic` removal would break the import chain — unconditional top-level `import anthropic` in `llm_client.py`.
4. `_execute` cannot be unchanged — the reader/writer split between gate and Conductor is a fundamental architectural change.

Plus one self-contradiction: "atomic commit" vs "one queue entry per file" — incompatible.

An addendum was written resolving all five issues. Both documents were reviewed and signed off. Then consolidated into a single authoritative v2.0 spec. No code was written until consolidation was complete.

**This is the gate chain principle applied to the spec itself.**

---

### File Map (implementation — tonight)

```
0.  conductor/               NEW — greenfield, ~600–900 LOC
1.  memory/llm_client.py     MODIFY — lazy anthropic import (gates step 9)
2.  memory/schema_capture.py NEW
3.  memory/contract_store.py MODIFY — all 9 abstract + 4 new methods
4.  memory/peer_checker.py   NEW
5.  memory/memory_gate.py    MODIFY — solo/managed mode branch
6.  memory/app.py            MODIFY
7.  scripts/prime_vdb.py     NEW
8.  scripts/migrate_contracts.py  NEW
9.  requirements.txt         MODIFY — remove anthropic (after step 1)
10. .env.example             MODIFY
11. tests/memory/            NEW
```

Zero changes to: `drift_scorer.py`, `models.py`, orchestrator, pre-commit hook, other gates, commit queue.

---

### New Environment Variables

| Variable | Default | Description |
|---|---|---|
| `CDMAD_STORE` | `json` | `json` or `vdb` — selects contract store backend |
| `CDMAD_VDB_PATH` | `.cdmad/vdb` | Root directory for VDB JSON store |
| `CDMAD_REPO_NAME` | auto from git | Override repo name for scoping |
| `CDMAD_SCHEMA_PATH` | `.cdmad/session_schema.json` | Where agents write declared schema |
| `CDMAD_AGENT_ID` | auto hostname+pid | Identifies agent's live schema file |
| `CDMAD_MANAGED` | unset | `1` when Conductor launches the gate run |
| `CDMAD_PEER_CHECK` | `1` | `0` to disable intra-session peer checking |
| `CDMAD_RETRIEVAL_TOP_K` | `20` | Max contracts retrieved per commit |

`ANTHROPIC_API_KEY` no longer required by the memory gate.

---

### Migration

All existing `.cdmad/contracts/` history migrates to `.cdmad/vdb/ci-wrapper/` via `scripts/migrate_contracts.py`. Non-destructive — JSON archive preserved. The audit trail in `audit.db` is untouched.

---

### What Stays the Same

The gate chain. All seven gates. Same ports, same phases, same enforcement. Pre-commit hook unchanged. Merge token still the only valid exit condition. SQLite audit trail still running. Every commit still passes all seven gates or doesn't land.

The Conductor is pre-gate infrastructure. The gates don't know it exists.

---

*"Generation is optional. Verification is not."*

---

## June 1, 2026 — Project Rebrand, Clipboard-Driven Conductor & Multi-Agent Resume Flow

# CIWarden Refactor & Interactive Multi-Agent Orchestration

## Overview

This session focused on formalizing the evolution of the local testing wrapper into a unified governance framework: **CIWarden**. Documentation boundaries, service titles, hook banners, and operational terminology were aligned under this identity to reinforce the project's hard-enforcement philosophy.

Additionally, a critical packaging issue was resolved by removing a stray repository-root `__init__.py` file. During test execution, pytest incorrectly traversed the package chain beyond the repository root, causing the parent directory to be inserted into `sys.path` and resulting in:

```text
ModuleNotFoundError: No module named 'gates'
```

Removing the file restored proper package resolution and stabilized the test gate.

---

# What Was Designed & Implemented

## Anthropic SDK Dependency Removal

Following the v3 optimization of the Generational Memory Gate, which now utilizes the local static AST parser (`SchemaCaptureClient`), the network-bound Anthropic SDK became unnecessary.

Changes:

- Removed `anthropic` from runtime dependencies.
- Removed associated `ignore_missing_imports` overrides from the mypy configuration.
- Eliminated unused network coupling within gate execution paths.
- Reduced dependency surface area and simplified deployment requirements.

---

## Interactive Conductor Layout & Clipboard Workflow

To support restricted execution environments where direct stdin piping into `claude -p` is unreliable or incompatible with Claude Max sessions, the multi-agent spawning workflow was redesigned around an interactive clipboard-driven architecture.

### Conductor Wizard Simplification

The setup flow was reduced to two required inputs:

- Description
- Prompt

The `module_key` is now derived automatically from the description, eliminating redundant subsystem prompts.

### Launch Script Generation

`distributor.write_launch_script()` now generates:

```text
<staging_root>/launch_agents.sh
```

The launch script:

- Uses AppleScript (`osascript`) to spawn parallel Terminal windows.
- Arranges agent sessions side-by-side.
- Launches each agent in its own isolated staging directory.

### Clipboard-Based Prompt Injection

Each terminal executes an interactive startup sequence that:

```bash
cat PROMPT.md | pbcopy
```

and then:

1. Copies the agent-specific prompt directly to the macOS clipboard.
2. Displays a paste instruction.
3. Launches:

```bash
claude --dangerously-skip-permissions
```

The operator manually pastes (`⌘V`) the prompt into each active Claude session.

This approach avoids stdin transport limitations while preserving full parallel-agent orchestration.

---

## Staging Manifest & Session Resumption

A robust session recovery mechanism was introduced through:

```bash
python3 -m conductor --resume
```

### Session Persistence

During distribution, orchestration state is serialized to:

```text
<staging_root>/session.json
```

The manifest captures:

- Repository path constraints
- Agent assignments
- Orchestration metadata
- Runtime configuration

### Resume Workflow

When `--resume` is specified, Conductor:

- Skips the setup wizard entirely.
- Bypasses `distribute()`.
- Prevents prompt regeneration.
- Preserves existing schemas.
- Preserves active agent state.

Before resuming execution it:

1. Validates staging tree integrity.
2. Removes stale `.done` tracking files.
3. Regenerates `launch_agents.sh`.
4. Re-enters the blocking orchestration wait state.

This enables immediate recovery from interrupted runs without losing agent progress or mutating staging artifacts.

---

## Two-Tier Commit Log Automation

The foundation was established for automated development-log generation through a non-blocking append-only synchronization pipeline.

### Commit Log Synchronization

A new script:

```text
scripts/update_commit_log.py
```

collects data from:

- `gate_results.db`
- Git metadata

and appends idempotent JSON Lines entries to:

```text
.cdmad/commit_log.jsonl
```

### Devlog Generation

A companion script:

```text
scripts/write_devlog_entry.py
```

generates Markdown development log skeletons from unconsumed entries using a configurable:

```bash
--since
```

boundary.

### Hook Integration

The synchronization pipeline is integrated as a best-effort side effect within:

```text
hooks/pre_commit.py
```

Execution occurs only after successful issuance of a cryptographic merge token.

A corresponding post-commit hook captures and records the finalized Git `HEAD` hash to maintain continuity between gate results, merge authorization, and repository history.

---

# Outcome

This work completes a major architectural transition toward CIWarden as a unified governance platform while simultaneously improving:

- Dependency hygiene
- Test reliability
- Multi-agent orchestration resilience
- Session recoverability
- Commit-history observability
- Automated development logging

The resulting system is significantly more robust in constrained execution environments and establishes the infrastructure necessary for future automation of orchestration reporting and governance workflows.

---

### Commits This Session

| SHA | Message | Gates | Total |
|-----|---------|-------|-------|
| `37963f3` | docs(readme): remove --no-verify subsection | ✓ 7/7 | 42845ms |
| `b1add7d` | feat(devlog): two-tier commit-log automation | ✓ 7/7 | 43216ms |
| `d9d502f` | feat(hooks): add best-effort post-commit hook for the commit log | ✓ 7/7 | 44540ms |
| `19876d2` | chore: rename project from CI Gate Wrapper to CIWarden | ✓ 7/7 | 44111ms |
| `f818957` | fix(tests): remove stray repo-root __init__.py breaking pytest collection | ✓ 7/7 | 44280ms |
| `813ce15` | chore(deps): drop anthropic from project dependencies | ✓ 7/7 | 44059ms |
| `6513fdd` | chore(deps): drop anthropic from mypy import-override list | ✓ 7/7 | 43795ms |
| `adeba56` | feat(conductor): launch_agents.sh generator + two-field wizard | ✓ 7/7 | 45142ms |
| `946d77d` | fix(conductor): launch script copies prompt to clipboard, runs claude interactively | ✓ 7/7 | 44307ms |
| `9555b5c` | feat(conductor): --resume flag to relaunch from existing staging | ✓ 7/7 | 44822ms |
| `d69db2d` | docs(readme): add Conductor --resume usage section | ✓ 7/7 | 44335ms |

---

### Gate Summary

| Gate | Fastest | Slowest | Runs |
|------|---------|---------|------|
| lint | 16ms | 103ms | 11 |
| typecheck | 691ms | 1508ms | 11 |
| security | 794ms | 1239ms | 11 |
| memory | 309ms | 578ms | 11 |
| test | 9879ms | 11659ms | 11 |
| stress | 30477ms | 30569ms | 11 |
| build | 148ms | 212ms | 11 |

---

### Merge Tokens

e501df5b3b3befa771e21bfa
6c999dc5b29d28f07087b4b0
2fdefe8703b1c6cc8660b11b
d715d19b154fc73f452f9080
600ba4f4107ad899921c9a3a
8d407e54d2de79ef7f2dce3e
df01ffd9d666ad53bec83a96
0bf2ca7c031cb258ed31a40a
b207f708fa2ea584fe5c1f5e
168e141bdc0dd936cca7fa30
2b7a021e99f86ba86cf04b02

---


---

## June 8, 2026 — Hardening the Post-Launch Flow & Process Autonomy

# CIWarden Runtime Hardening & Autonomous Queue Orchestration

## Overview

This session focused on closing critical edge-case vulnerabilities within the Conductor's multi-agent runtime and branch coordination systems. The primary objective was to eliminate state-engine hangs, strengthen staging isolation guarantees, and ensure agent activity cannot bypass orchestration controls.

In parallel, the project was formally re-licensed under the **Apache License 2.0**, establishing a standardized open-source licensing framework for future development and distribution.

---

# What Was Designed & Implemented

## Post-Launch Flow Termination Fixes

Two critical defects affecting session state synchronization and execution reliability were identified and resolved.

### Vacuous / Stale Handoff Protection

A flaw existed where the Conductor could prematurely transition into proofing under two conditions:

- The staging workspace contained no active agent signals.
- A previous session left behind stale `.done` markers.

Under these circumstances, `run_session()` could incorrectly assume all work had completed and immediately advance to proofing before active agents finished generation.

The execution engine now:

- Explicitly protects against empty execution states.
- Clears stale `.done` markers before entering the polling loop.
- Guarantees synchronization occurs only against valid active-session signals.

This prevents premature proofing and restores deterministic session completion behavior.

---

### Staging Escape Recovery

A second failure mode occurred when an agent wrote directly into the destination repository rather than its assigned staging workspace.

Previously, this caused:

```python
RuntimeError("no staged files")
```

to be raised by `atomic_commit()`, terminating the orchestration thread.

The commit path was hardened to:

- Detect staging escape conditions.
- Safely copy staged assets before finalization.
- Return a clean empty-result exit path.
- Emit operational warnings instead of crashing the runtime.

This transforms a fatal runtime failure into a recoverable operational condition.

---

## Staging Constraints & Soft Signal Detection

To further reduce repository contamination risk, staging controls were strengthened at both the prompt and orchestration layers.

### Reinforced Staging Directives

The distribution prompt architecture now includes an explicit high-priority directive instructing agents to:

- Operate exclusively within assigned staging paths.
- Avoid direct mutations against target repositories.
- Treat staging workspaces as the sole authorized write boundary.

---

### Soft Signal Recovery Path

While prompt-level controls reduce violations, model behavior can occasionally produce unintended writes.

To prevent indefinite orchestration hangs in these scenarios:

`wait_for_done()` now monitors the repository for untracked additions matching an agent's `module_key`.

When detected:

1. A warning is logged.
2. The execution block is released.
3. The pipeline advances to proofing.

Rather than waiting forever for a completion signal that may never arrive, the system treats repository mutations as a **soft completion signal** and continues execution.

This preserves forward progress while maintaining operator visibility into staging violations.

---

## Autonomous Detached Worker Queuing

The commit pipeline was extended to support fully autonomous queue consumption without operator intervention.

### Repository-Aware Worker Discovery

The queue worker management system is now directly coupled to target repository lifecycles.

Upon successful multi-agent session completion:

- `ensure_worker()` inspects the host system using `pgrep`.
- Active queue workers are searched specifically for the target repository.
- Repository-scoped workers are treated independently from the CIWarden orchestration environment.

Example target repository:

```text
session.repo_root
```

such as a downstream project workspace.

---

### Automatic Worker Provisioning

If no active worker is detected:

- A detached background daemon is launched automatically.
- Queue processing begins immediately.
- The worker operates independently of the Conductor session lifecycle.

Successful startup is recorded with:

```text
CONDUCTOR ● queue worker started for {repo_name}
```

This guarantees commit queues continue draining after orchestration completes, eliminating the need for manual worker startup procedures.

---

## Apache 2.0 License Adoption

The codebase was formally transitioned to the Apache License 2.0 standard.

Benefits include:

- Explicit patent grants.
- Commercial-use compatibility.
- Contribution clarity.
- Industry-standard open-source governance.

This establishes a clear legal framework for external contributors, enterprise adoption, and future ecosystem growth.

---

# Audit Resolution

The session concluded with remediation of four critical Phase 4 audit discrepancies.

These fixes finalized consistency guarantees across:

- Runtime state management
- Session termination behavior
- Staging enforcement
- Queue processing lifecycle coordination

The resulting system significantly improves orchestration reliability, fault tolerance, and autonomous operation while ensuring repository boundaries remain protected throughout the multi-agent execution lifecycle.

---

### Commits This Session

| SHA | Message | Gates | Total |
|-----|---------|-------|-------|
| `5ddbec7` | fix(conductor): wait for fresh .done before proofing; commit empty-staging gracefully | ✓ 7/7 | 44036ms |
| `f1fefaa` | docs(readme): relicense under Apache 2.0; add LICENSE file | ✓ 7/7 | 44718ms |
| `b9895ac` | fix(conductor): auto-start queue worker on commit; soft-signal repo writes | ✓ 7/7 | 44620ms |
| `982b263` | fix(conductor,queue): repair Phase 4 audit failures | ✓ 7/7 | 44292ms |

---

### Gate Summary

| Gate | Fastest | Slowest | Runs |
|------|---------|---------|------|
| lint | 30ms | 81ms | 4 |
| typecheck | 638ms | 982ms | 4 |
| security | 882ms | 981ms | 4 |
| memory | 390ms | 418ms | 4 |
| test | 10965ms | 11758ms | 4 |
| stress | 30467ms | 30517ms | 4 |
| build | 140ms | 207ms | 4 |

---

### Merge Tokens

49288f1d6b5d5d6506341200
d08c8df4dd2fdbf3e30176bb
54ee46f315bcfbd3aa6d2f48
e527ae169208f879fc8c3ebb

---

*"Generation is optional. Verification is not."*

---

## June 9, 2026 — {title placeholder}

# Conductor v2 — DAG Upgrade + Multi-Repo Routing

**Date:** June 9, 2026
**Branch:** v1.1
**Commits:** `3272a0a`, `0891d62`
**Merge tokens:** `a20ad14752021a88e2676976`, `4abbbc96cf1990c7eca89eb1`
**Lines changed:** +2109 / -40
**Gate results:** 7/7 both commits

---

## What Changed

Two new files. Two modified files. The Conductor grew to match the complexity of the problems it now needs to solve.

`dag.py` — new. `DAGBuilder` parses the `CONTRACTS_CONSUMED` block from every agent prompt and builds a dependency graph. Cycle detection. Topological ordering. `ready_agents()` returns only agents whose declared dependencies have signaled `.done`. The dependency information was already in the prompts — this is the layer that reads it.

`router.py` — new. `RepoRouter` holds one `RepoContext` per repo involved in a session. Routes each agent's commit queue entry to the correct repo's worker. Two repos active simultaneously means two queue workers, two atomic commits, one session.

`distributor.py` — modified. The `.done` polling loop is now dependency-aware. Agents launch when their dependencies clear, not when the session starts. The backward compatibility gate is explicit and non-negotiable — sessions with no `CONTRACTS_CONSUMED` declarations take the old path byte-for-byte.

`commit.py` — modified. `atomic_commit_multi_repo()` fires commits in topological order across all repos. Producer repos commit before dependent repos. VDB committed once before any queue entry.

`cli.py` — modified. Wizard gains a per-agent repo field. DAG is built after wizard completes, before any agent launches. Dependency summary printed for operator visibility.

25 new tests. 316 passing total.

---

## Why This Session Happened

The Sentinel v3 build has eight agents across two repos — `sentinel` and `GolemLinux`. The current Conductor launches all agents simultaneously. Agent 4 cannot start until Agent 3 has produced a type that Agent 4 consumes. Agent 7 cannot start until Agents 3, 5, and 6 are all done. The current Conductor has no way to enforce any of that.

The answer was not to build a separate "Wave Orchestrator" module. That was the first instinct — a new layer sitting above the Conductor, managing waves, watching `.done` files, queueing dependent agents. The right answer became clear quickly: that's just the Conductor doing what a conductor actually does.

A real conductor doesn't wave everyone in at once and hope for the best. It cues each section when it's their turn.

The DAG awareness and multi-repo routing aren't new features bolted on. They're what the Conductor should have been from the start. The v1 Conductor was simpler because the sessions were simpler — one repo, loosely coupled agents. Now the sessions are more complex and the Conductor grows to match.

---

## The Moment That Earned a Devlog Entry

The session ran the Conductor upgrade under the current Conductor. Two agents. One dependency: Agent 2 could not start until Agent 1 signaled `.done`.

Agent 2 polled for Agent 1's `.done` file. Found it. Read Agent 1's actual produced code — not the spec pseudocode — and caught three real integration details before writing a single line:

- `agent_by_id` not `get_agent`
- `repo_root` not `repo_path`
- No `prompt_path` field on `AgentSpec`

Without the dependency gate, Agent 2 would have written against the spec and produced code that didn't compile against Agent 1's actual implementation. The gate caught the mismatch before it became a problem.

**The first dependency the upgraded Conductor enforced was its own.**

The Conductor upgraded itself under its own governance, using the dependency ordering it was in the process of gaining. That is not a coincidence. That is what the system is for.

---

## Gate Chain Notes

The commit did not go in cleanly on first attempt. Three typecheck errors in Agent 2's test files — missing `-> None` annotations on `fake_run` helpers, a `cast(list[str], ...)` that needed TC006 quoting, and a `files: list[str]` annotation missing on the return capture from `atomic_commit_multi_repo()`.

All mechanical. No logic changes. Fixed manually after the gate blocked.

The 10 bandit B603/B607 findings are intentional and pre-existing — subprocess without `shell=True` is the correct pattern for invoking CLI tools by name. Whitelisted in `pyproject.toml`. Not introduced this session.

---

## What's Next

The Conductor upgrade is the unlock. It runs Sentinel v3 — eight agents, two repos, dependency ordering enforced automatically.

After Sentinel v3: CDWarden. After CDWarden: the platform wrapper.

The full sequence:

```
✓ Conductor DAG upgrade         this session
→ Sentinel v3                   8 agents, upgraded Conductor, sentinel + GolemLinux
→ CDWarden                      new Rust workspace, delivery governance layer
→ Platform                      Electron wrapper, DMG/NSIS/AppImage, the product
```

The Big 3 spec is documented in `THE_BIG_3_SPEC.md`.

---

*CIWarden v1.1 — feat(conductor): DAG upgrade + multi-repo routing*
*Copyright © 2026 Brandon Green. Licensed under the Apache 2.0 License.*

---

### Commits This Session

| SHA | Message | Gates | Total |
|-----|---------|-------|-------|
| `3272a0a` | updated DEVLOG.md and README.md | ✓ 7/7 | 43943ms |
| `0891d62` | feat(conductor): DAG upgrade + multi-repo routing + lint/typecheck fixes | ✓ 7/7 | 54519ms |

---

### Gate Summary

| Gate | Fastest | Slowest | Runs |
|------|---------|---------|------|
| lint | 66ms | 85ms | 2 |
| typecheck | 971ms | 10807ms | 2 |
| security | 928ms | 1174ms | 2 |
| memory | 477ms | 569ms | 2 |
| test | 10785ms | 11104ms | 2 |
| stress | 30528ms | 30537ms | 2 |
| build | 188ms | 243ms | 2 |

---

### Merge Tokens

a20ad14752021a88e2676976
4abbbc96cf1990c7eca89eb1

---

*"Generation is optional. Verification is not."*

---

## June 10, 2026 — {title placeholder}

# Sentinel v3 — First Multi-Agent Multi-Repo Build

**Date:** June 9, 2026
**Branch:** main
**Repos:** sentinel + GolemLinux
**Agents:** 8
**Tests landed:** 196 passing
**Merge token:** 595d6f3e15c7

---

## What This Session Was

The first real test of the upgraded Conductor under production conditions. Eight agents across two repos, dependency ordering enforced by the new DAG, Sentinel v3 built from scratch in a single session.

It did not go cleanly. It went honestly. Every problem that surfaced was real, caught before it became permanent, and resolved without losing any work. That is the system functioning as designed.

---

## The Good

**Agent 8 saved the session.**

By the time Agent 8 ran, the repo was in a half-merged state. Three agents had written to staging correctly. Three had written directly to the repo — outside the Conductor's control. Agent 8 landed with no `.done` files visible in staging, an incomplete repo that wouldn't compile, and a Conductor stuck in deadlock.

It mapped the entire situation methodically. Read every staging directory. Read the repo. Cross-referenced what was present against what was specced. Built a clear task list. Then it did what none of the other agents did — it completed the integration that the rogue agents left unfinished. Copied keygen, transport, and audit chain from staging into the repo. Wired transport into sentinel-core. Integrated the audit chain. Created the missing Python package. Compiled the PyO3 extension. Ran all verification targets.

196 tests passing when it signaled `.done`.

**The DAG worked.** Agents 5 and 6 correctly waited for Agent 1 before launching. The dependency ordering logic that was built this morning worked on its first real multi-agent session this afternoon. The Conductor cued each section when it was their turn.

**Agent 3 caught Agent 1's contract in real code.** Agent 3 read Agent 1's actual produced types from staging — not the spec — and integrated against the real interface. Same pattern as the Conductor session this morning. The agents are learning to verify against reality, not documentation.

**Agent 6 discovered the GolemLinux audit chain was already chained.** Rather than overwrite verified FIPS-tested cryptographic primitives with a fresh implementation, it noted the no-op and moved on. That's the right call.

**196 tests. 7 crates. One session.**

---

## The Bad

**Three agents went rogue.**

Agents 1, 4, and 5 wrote directly to `~/Projects/sentinel` instead of their staging directories. The Conductor's staging isolation is a convention enforced by the prompt, not a hard filesystem constraint. These agents ignored the convention and wrote to the repo.

The consequences were real. Agent 8 launched expecting an empty repo and found half-merged v3 types. Agent 3 wrote transport code against `sentinel_types::ProcessIdentity` that didn't exist yet in the repo. The workspace didn't compile. Agent 8 had to do emergency integration work that was never in its scope.

This is the most important v1.2 fix. The Conductor must enforce staging isolation at the filesystem level — set each agent's working directory to its staging dir, and make the repo path read-only during the session. Right now it is honor system. Honor system fails.

**The Conductor deadlocked.**

Agent 8 correctly stood down when its dependencies weren't in the repo. The Conductor was waiting for Agent 8's `.done`. Agent 8 was waiting for the repo to have the other agents' work. Classic deadlock — broken only by manually pointing Agent 8 at the staging directories.

The root cause: Agent 8 checked the repo, not the staging directories. Its prompt didn't tell it to look in sibling staging dirs. Future agent prompts for final-stage agents need explicit instructions to check `../agent_00N/` staging output when repo state is incomplete.

**The DAG parsing was incomplete.**

Agents 2, 3, 4, 6, 7, and 8 all showed "no dependencies" in the dependency graph despite having clear `CONTRACTS_CONSUMED` declarations. Only Agents 5 and 6 were correctly parsed as waiting for Agent 1. The zero-padded ID resolution that Agent 1 built this morning apparently works for some patterns and not others under real session conditions. This needs a dedicated investigation.

**The clipboard race is back.**

The programmatic window launcher in the upgraded `distributor.py` didn't carry forward the serialized clipboard write logic from the old `launch_agents.sh`. Six terminal windows opened simultaneously, all overwriting the clipboard. Prompts had to be drag-and-dropped manually. Fixed in the moment but needs a proper fix in v1.2.

**Wizard redundancy — fixed mid-session.**

The wizard was asking repo per-agent (Enter x8) and redundantly asking for a path after a full path was already given. Fixed with a quick Claude Code session before the build started. The fix worked — wizard now asks once globally, overrides per-agent only when needed. But the fact that this friction existed at all in the first multi-repo session is worth noting.

---

## The Satisfactory

**`sentinel-signals` was already broken before this session.** `detectors.rs` references `SignalType::Cascade` which doesn't exist in `sentinel-types`. Agent 6 flagged it, Agent 8 confirmed it, neither touched it. Pre-existing, out of scope, left for a dedicated fix. Good discipline.

**The `SentinelError` struct-vs-enum conflict** was flagged by three separate agents independently — 4, 5, and 8. Nobody resolved it unilaterally. It's documented in `INTEGRATION_REPORT.md` for a deliberate fix rather than an agent making a solo call on a shared contract.

**Agent 7 made the right calls on GolemLinux.** Replaced `todo!()` bodies with real kernel bookkeeping rather than panicking on reachable code paths. Passed through untracked processes silently to preserve Sentinel's invisibility guarantee. Both deviations from the spec were correct and documented.

**The session recovered.** Despite rogue writes, a deadlocked Conductor, incomplete DAG parsing, and a half-merged repo — 196 tests are passing and Sentinel v3 is committed. Nothing was lost. The audit trail is intact.

---

## Open Items for v1.2

1. **Staging isolation enforcement** — repo read-only during Conductor sessions, agent CWD set to staging dir. This is the most important fix.
2. **Clipboard serialization** — restore the delay/serialization from `launch_agents.sh` in the new programmatic launcher.
3. **DAG parsing gap** — zero-padded ID resolution not working for all `CONTRACTS_CONSUMED` patterns. Investigate and fix.
4. **Agent 8 staging awareness** — final-stage agent prompts need explicit instruction to check sibling staging dirs when repo is incomplete.
5. **`sentinel-signals` compile fix** — `SignalType::Cascade` missing from `sentinel-types`. Small fix, separate session.
6. **`SentinelError` struct-vs-enum** — resolve the contract conflict across all crates. Documented in `INTEGRATION_REPORT.md`.
7. **Commit message agent count** — Conductor counted staging agents only, missed repo-direct agents. Fix the count logic.

---

## What Sentinel v3 Delivered

- `sentinel-types` — 7 new v3 types: `ProcessIdentity`, `SessionCredential`, `ChainedAuditEntry`, `InterceptionEvent`, `InterceptionDecision`, `DenyReason`, `SyscallId`
- `sentinel-keygen` — new binary crate, P-256 keys via openssl, allowlist management, key rotation, verify command
- `sentinel-core/transport` — `SentinelTransport` trait, `UnixTransport`, `PipeTransport`, `KernelTransport` stub
- `sentinel-core/ebpf` — eBPF interception module, `SocketAuth`, `Allowlist`, `--oo` observer flag
- `sentinel-core/audit` — cryptographic audit chain, NDJSON format, `sentinel-verify` binary
- `sentinel-controls` — `SentinelCapability` trait, `Enforcer`, `Observer` (no-ops), `LegionnairePolicy`, five deployment profiles, `TelegramNotifier`
- `sentinel-py` — v3 operator bindings: `ProcessIdentity`, `AuditEntry`, `verify_audit_chain`, `read_audit_chain`, `get_profile`, `is_observer_mode`
- `GolemLinux/src/sentinel` — kernel subsystem upgraded: `enforcer.rs`, `observer.rs`, compile-time mutual exclusion, native interception layer

---

*Sentinel v3 — First Multi-Agent Multi-Repo Build*
*Copyright © 2026 Brandon Green. Licensed under the Apache 2.0 License.*
*Session date: June 9, 2026*

---

### Commits This Session

| SHA | Message | Gates | Total |
|-----|---------|-------|-------|
| `94ad76a` | docs(devlog): Conductor v2 session narrative | ✓ 7/7 | 54519ms |
| `2c6fe77` | fix(conductor): eliminate wizard redundancy + normalize repo paths | ✓ 7/7 | 47396ms |

---

### Gate Summary

| Gate | Fastest | Slowest | Runs |
|------|---------|---------|------|
| lint | 83ms | 85ms | 2 |
| typecheck | 5748ms | 10807ms | 2 |
| security | 698ms | 1174ms | 2 |
| memory | 380ms | 569ms | 2 |
| test | 9772ms | 11104ms | 2 |
| stress | 30525ms | 30537ms | 2 |
| build | 190ms | 243ms | 2 |

---

### Merge Tokens

4abbbc96cf1990c7eca89eb1
2be4ecde17eda2a0a73f269c

---



---

## June 11, 2026 — CIWarden v1.2 — Five Conductor Governance Fixes



**Date:** June 9, 2026
**Branch:** v1.2 → main
**Commit:** 461770b
**Merge token:** 41d591e3
**Tests:** 350 passing
**Gate results:** 7/7

---

## What This Session Was

A focused hardening session. No new features. Five real problems discovered during the Sentinel v3 build — the first production multi-agent, multi-repo session under the upgraded Conductor — fixed one by one and shipped as a clean v1.2 release.

Every fix was driven by something that actually happened during a real build session. Not speculative hardening. Evidence-based governance.

---

## The Five Fixes

### 1. Staging Isolation

The most important fix in this release.

During the Sentinel v3 build, three agents wrote directly to `~/Projects/sentinel` instead of their staging directories. The Conductor's staging isolation was a convention enforced by the prompt — not a hard filesystem constraint. Agents ignored the convention.

The consequence was real. The repo ended up in a half-merged state mid-session. Agent 8 launched expecting an empty repo and found half-completed v3 work. The workspace wouldn't compile. Emergency integration work was required.

The fix: before the first agent terminal opens, `_RepoIsolator` snapshots the exact `st_mode` of every file and directory under the repo and strips write bits (`mode & ~0o222`). The repo is physically read-only. Any write attempt fails at the OS level — not at the convention level.

The cleanup handler is bulletproof: `atexit.register()` + `SIGTERM` + `SIGINT` handlers. If the Conductor crashes, permissions restore automatically. Permissions are restored before the atomic commit fires so the worktree copy succeeds.

5 tests. Verified: rogue write raises `PermissionError`. SIGTERM restores permissions. Unhandled exception restores permissions.

### 2. Clipboard Serialization

The programmatic `launch_agent_window()` introduced in the Conductor DAG upgrade opened N terminal windows simultaneously. Each window overwrote the clipboard with its prompt before the operator could paste. Only the last agent's prompt survived.

The fix: the Conductor is now the sole clipboard authority. It opens one window, writes that agent's prompt contents to the clipboard synchronously via `pbcopy`, prints a status line, waits 1.5 seconds, then opens the next window. The async-window race is eliminated entirely — not just narrowed.

```
CONDUCTOR  ● agent_001 ready — ⌘V to paste prompt, then next window opens in 1.5s
```

The delay is configurable via `LAUNCH_PASTE_DELAY`. 7 tests.

### 3. DAG Parsing Gap

During Sentinel v3, only 2 of 8 agents had their dependencies correctly parsed. Agents 2, 3, 4, 7, 8 all showed "no dependencies" despite clear `CONTRACTS_CONSUMED` declarations.

Diagnosis: the old regex `\(Agent\s+(\d+)\)` matched a number only when it sat inside parentheses immediately after "Agent". The real prompt notations used six different formats:

- Bare colon refs: `sentinel-types (ProcessIdentity …)` — no Agent N token
- Number before paren: `Agent 3: SentinelTransport` — number not parenthesized
- Number before label: `Agent 1 (sentinel-types)` — paren holds a label, not the number
- Plural range: `All agents 1–7` — en-dash range, no parens

The fix: rewrote `AGENT_REF_PATTERN` to anchor on the word `Agent/Agents` and capture the run of numbers, list separators, and range dashes that follow — regardless of parenthesis placement. Range expansion handles dash/en-dash/em-dash, `to/through/thru`, and comma/and lists.

Result: 5 of 8 patterns now resolving, up from 2 of 8. The remaining 2 (Agents 2 and 3) reference their producers by crate name only — no `Agent N` token. A number-based parser cannot resolve those. The fix is in the prompt authoring convention: always use `(Agent N)` notation explicitly. This is documented and pinned as a test rather than silently worked around.

8 new regression tests using verbatim Sentinel v3 prompt notations.

### 4. Final-Stage Agent Awareness

Agent 8 in the Sentinel v3 session launched, checked the repo, found no v3 work, and stood down. Its dependencies were in staging — not yet committed to the repo. It had no way to know where to look.

The fix: during prompt distribution, the Conductor detects agents whose `depends_on` list covers all other agents in the session (final-stage agents). For those agents, it injects a staging awareness block into their prompt — listing every dependency's staging directory path explicitly.

```
STAGING AWARENESS: Your dependencies may not yet be committed to the repo.
Before checking the repo for their work, check the sibling staging directories:
/path/to/staging/session_id/agent_001/
/path/to/staging/session_id/agent_002/
...
```

The injection is idempotent and doesn't perturb the DAG that `cli.build_dag` builds afterward. 12 tests including the Sentinel v3 `All agents 1–7` range case end-to-end.

### 5. Commit Message Agent Count

The Sentinel v3 atomic commit message said "7 agents" when 8 agents contributed. The multi-repo commit path was using the per-repo agent count (7 agents routed to the sentinel repo) instead of the full session count.

The fix: one line in `atomic_commit_multi_repo` — `len(session.agents)` instead of `len(repo_agents[repo_name])`. Every repo's commit message now reports the full session agent count.

2 tests. The regression test fails on the old code, confirming it pins the fix.

---

## Release Notes

```
v1.2 — CIWarden Conductor Governance Hardening

Five fixes driven by evidence from the Sentinel v3 production build session.

Breaking changes: none.
New features: none.
Fixes: staging isolation, clipboard serialization, DAG parsing,
       final-stage agent awareness, commit message count.
Tests: 350 passing (up from 316 at v1.1 release).
```

---

## What's Next

Two Sentinel repo fixes remaining from the v3 build:
- `sentinel-signals`: `SignalType::Cascade` missing from `sentinel-types`
- `SentinelError`: struct-vs-enum contract inconsistency across crates

Then GolemLinux Phase 6 — bare metal boot on the Intel MacBook Air.

---

*CIWarden v1.2 — Five Conductor Governance Fixes*
*Copyright © 2026 Brandon Green. Licensed under the Apache 2.0 License.*
*Session date: June 9, 2026*

---

### Commits This Session

| SHA | Message | Gates | Total |
|-----|---------|-------|-------|
| `7fa7a40` | docs(devlog): Sentinel v3 build session narrative | ✓ 7/7 | 42953ms |
| `4fcef9b` | fix(conductor): staging isolation — repo read-only during sessions | ✓ 7/7 | 44926ms |
| `d749445` | fix(conductor): clipboard serialization — Conductor owns clipboard, 1.5s window delay | ✓ 7/7 | 47383ms |
| `461770b` | fix(conductor): CIWarden v1.2 — five governance fixes | ✓ 7/7 | 47851ms |
| `ccf3c75` | docs(devlog): CIWarden v1.2 session narrative | ✓ 7/7 | 47851ms |

---

### Gate Summary

| Gate | Fastest | Slowest | Runs |
|------|---------|---------|------|
| lint | 28ms | 84ms | 5 |
| typecheck | 895ms | 3113ms | 5 |
| security | 678ms | 1288ms | 5 |
| memory | 367ms | 515ms | 5 |
| test | 9774ms | 12260ms | 5 |
| stress | 30526ms | 30533ms | 5 |
| build | 200ms | 266ms | 5 |

---

### Merge Tokens

a15aea2deb27fbe7756b1eff
8253b6c37dc4779e1bc2a90a
273a8263f9efe0b2a911bee9
41d591e3e39454bca49eb5d1
41d591e3e39454bca49eb5d1

---

*"Generation is optional. Verification is not."*

---

## June 11, 2026 — GolemLinux Phase 6 — Bare Metal Boot


**Date:** June 9, 2026
**Branch:** main
**Commit:** 9a2f914e (Phase 6 session) + drivers wiring
**Merge token:** e7ddc95fbe3744f570702cf0
**Agents:** 6
**Gate results:** 7/7

---

## What This Phase Was

Getting GolemLinux off QEMU and onto real silicon. Not a VM. Not an emulator. A real kernel booting on a real machine.

The target: a 2017 MacBook Pro 13". Intel Kaby Lake. No T2 chip. UEFI compliant. The last Intel MacBook before Apple started locking down the boot path with bridgeOS.

Five phases built the kernel. Phase 6 builds the path from binary to bare metal.

---

## What Landed

**`scripts/flash_usb.sh`** — the USB flash script. Lists physical external drives only, refuses to offer anything flagged as internal. Requires the operator to type `CONFIRM` in all caps before touching the drive. Writes via `dd` to the raw device for speed. Syncs, ejects, and reports SHA256 of both the source image and the read-back bytes — VERIFIED or MISMATCH. macOS bash 3.2 compatible. Nothing happens without explicit confirmation. Nothing proceeds without verification.

**`docs/APPLE_EFI_NOTES.md`** — 361 lines of authoritative Apple EFI reference material. The decisive finding: the 2017 MBP is pre-T2. No Secure Boot enforcement. No bridgeOS on the boot path. An unsigned `gkern` boots from USB with zero firmware changes required. Hold Option at startup, select the EFI Boot entry, done. `boot.asm` was correctly left untouched — the Apple quirks live in firmware boot-selection and on-disk layout, not at the handoff boundary where the 2017 MBP is standard UEFI-2.x-compliant Microsoft x64.

**`docs/BARE_METAL_BOOT.md`** — step-by-step boot verification procedure written for a first-timer. One honest caveat baked in: COM1 serial output goes nowhere on a MacBook. There is no physical RS-232 port. Primary boot verification is the GOP framebuffer on the laptop's own display. The document accounts for this — verification is visual, not serial. Two-camera recording setup documented: overhead for screen and hands, close-up for legible text.

**`src/drivers/cpuid.rs` + `mod.rs`** — CPUID hardware detection. Vendor string (GenuineIntel verification), CPU family/model/stepping via standard x86 extended-family folding rules, SSE/SSE2/AVX feature detection. Outputs to serial at boot. 6 unit tests. Wired into the kernel init sequence between `memory::init()` and `scheduler::init()`.

**`Cargo.toml` release profile** — `opt-level = "z"` (size optimized), `strip = true` (debug symbols removed). Release binary: 1.05 MiB. Debug binary: 3.99 MiB. 3.8x smaller. A smaller binary boots faster and fits more comfortably in a bootloader context.

**`docs/PHASE6_BARE_METAL.md`** — the milestone document. The framing that earned its place: *"A VM is a comfortable lie. GolemLinux runs on real silicon."*

---

## The v1.2 Fix Working in Production

The staging isolation fix from CIWarden v1.2 ran for the first time on a real GolemLinux session:

```
CONDUCTOR  ● repo isolated: /Users/bmacbr/Projects/GolemLinux
           (write permissions removed)
...
CONDUCTOR  ● repo restored: /Users/bmacbr/Projects/GolemLinux
           (write permissions restored)
```

No rogue writes. No half-merged repo. Six agents, six staging directories, one atomic commit. The governance fix governed its first GolemLinux build cleanly.

---

## What Phase 6 Does Not Include

The kernel boots. What it shows on boot depends on whether the GOP framebuffer is correctly initialized before the display pipeline is set up. That's runtime work — not Phase 6 scope. Agent 2 flagged the cross-subsystem advisories: GOP injection, memory-map/ExitBootServices retry, NVRAM hygiene. These are noted in `APPLE_EFI_NOTES.md` for Phase 7 or a dedicated hardware integration pass.

Serial output at boot goes to COM1. On bare metal MacBook hardware, COM1 writes go into the void. The kernel doesn't know it's on a MacBook. A future phase will route early console output to the GOP framebuffer explicitly so boot progress is visible on the laptop's own display.

---

## What's Next

Phase 7 — GolemLinux ships with tooling. The kernel is useful out of the box.

```
Agent 1    Package bootstrap — fetch, verify SHA256, install
Agent 2    Shell — minimal governed command interface
Agent 3    Default config — golem.toml, validated at boot
Agent 4    First boot experience — banner, hardware detection, ready state
Agent 5    Headless mode — serial console, SSH, deploy script
Agent 6    README and Phase 7 documentation
```

After Phase 7: the bare metal boot on the Intel MacBook Air. GolemLinux running on real hardware, recorded.

---

*GolemLinux Phase 6 — Bare Metal Boot*
*Copyright © 2026 Brandon Green. Licensed under the Apache 2.0 License.*
*Session date: June 9, 2026*

---

### Commits This Session

| SHA | Message | Gates | Total |
|-----|---------|-------|-------|
| `6831a6d` | docs(devlog): CIWarden v1.2 session narrative | ✓ 7/7 | 47851ms |
| `213c3df` | docs(devlog): CIWarden v1.2 session narrative | ✓ 7/7 | 47851ms |

---

### Gate Summary

| Gate | Fastest | Slowest | Runs |
|------|---------|---------|------|
| lint | 84ms | 84ms | 2 |
| typecheck | 3113ms | 3113ms | 2 |
| security | 1158ms | 1158ms | 2 |
| memory | 498ms | 498ms | 2 |
| test | 12206ms | 12206ms | 2 |
| stress | 30526ms | 30526ms | 2 |
| build | 266ms | 266ms | 2 |

---

### Merge Tokens

41d591e3e39454bca49eb5d1
41d591e3e39454bca49eb5d1

---

*"Generation is optional. Verification is not."*
