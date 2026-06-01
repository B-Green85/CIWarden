# DEVLOG — CI Gate Wrapper

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
