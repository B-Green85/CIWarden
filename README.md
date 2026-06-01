# CI Gate Wrapper

### CDMAD Enforcement Layer for Agentic Development

**Copyright (c) 2026 Brandon Green. All rights reserved.**

---

## The Problem

Agentic development is eclipsing CI.

AI agents generate code at a speed and volume that static analysis tools were never designed to govern. They write plausible-looking code that passes a linter, fails under load, drifts architecturally from session to session, and degrades silently in production — all without a human ever noticing until the damage is done.

The frameworks built to manage this are focused on making agents more capable. Nobody has built the layer that makes them more governable.

This is that layer.

---

## What It Is

The CI Gate Wrapper is a seven-gate enforcement chain that sits between code generation and merge. Every commit — human or agent — must pass all seven gates before a merge token is issued. No token, no merge. No exceptions. No bypasses.

It is the mechanical implementation of **CDMAD** — Constraint Driven Model Assisted Development. The principle is simple: generation is optional. Verification is not.

Agents generate. Gates verify. The merge token is cryptographic proof of passage — not a formality, a timestamped record that the full chain ran clean.

The orchestrator is the sole authority that issues merge tokens. Agents have no path around it.

---

## What Makes It Different

Most CI systems are static. They analyze code that already exists and tell you what is wrong with it. They run after the fact. They have no concept of what an agent decided while generating, no awareness of whether the new code contradicts what was built three sessions ago, and no ability to simulate how the system behaves under real load before it ships.

This system operates at three layers that no existing tool addresses:

**Architectural continuity.** The Memory Gate extracts contracts from every generation and scores semantic drift against prior generations. If a new component contradicts an established interface — different field names, different units, different assumptions — it is blocked before it can propagate downstream. The codebase has a shape. Every generation must honor that shape or explicitly declare a breaking change.

**Resilience under load.** The Stress Gate runs three-phase load simulation against every API integration before it can commit. NORMAL at 70% of rate limit. THRESHOLD at 100%. BREACH at 120%. It measures error rates, retry storms, cascade failures, and recovery time. Code that passes a linter but collapses under real traffic does not pass this gate.

**Hard enforcement.** The pre-commit hook is not a suggestion. It calls the live orchestrator and exits with code 1 on any gate failure. Git refuses the commit. The agent cannot proceed. The framework is designed to make bypassing the gate chain a deliberate act of technical debt, not an easy shortcut.

---

## Gate Chain

```
Agent / Developer Commit
         │
         ▼
  [PRE-COMMIT HOOK]
         │
         ▼
  [ORCHESTRATOR :8000]
         │
         ├── Phase 1 — Parallel ──────────────────────────────────┐
         │   ├── LINT      :8001  ruff         static analysis    │
         │   ├── TYPECHECK :8002  mypy         type safety        │
         │   ├── SECURITY  :8003  bandit       vulnerability scan │
         │   └── MEMORY    :8006  static AST   drift detection    │
         │                                                        │
         ├── Phase 2 — Sequential (requires Phase 1 PASS) ───────┤
         │   ├── TEST      :8004  pytest       unit/integration   │
         │   ├── STRESS    :8007  load runner  API resilience     │
         │   └── BUILD     :8005  py_compile   compilation        │
         │                                                        │
         └── All 7 gates PASS ──► MERGE TOKEN issued              │
                                  SHA256 + timestamp              │
                                  Persisted to SQLite audit trail─┘
```

### Gate Descriptions

| Gate | Port | Phase | Tool | Purpose |
|------|------|-------|------|---------|
| **lint** | 8001 | parallel | ruff | Static analysis, style enforcement, import ordering |
| **typecheck** | 8002 | parallel | mypy | Strict type safety — no implicit `Any`, no untyped defs |
| **security** | 8003 | parallel | bandit | Security audit — flags injection, hardcoded secrets, unsafe calls |
| **memory** | 8006 | parallel | static AST | Generational memory — extracts architectural contracts (static, no API call), scores drift against prior generations, blocks on drift >= 0.5 |
| **test** | 8004 | sequential | pytest | Unit and integration test suite with coverage enforcement |
| **stress** | 8007 | sequential | load runner | Three-phase API stress testing — NORMAL (70%), THRESHOLD (100%), BREACH (120%) of rate limit |
| **build** | 8005 | sequential | py_compile | Compilation validation — clean compile, no circular imports |

---

## Generational Memory Gate

The memory gate enforces **architectural continuity** across code generations. It prevents silent contract drift — the invisible divergence where field names shift, units change, and interface shapes break downstream consumers without anyone noticing until production.

**How it works:**

1. Extracts architectural contracts from source code via **static analysis** — an agent-declared schema (`.cdmad/session_schema.json`) when present, falling back to AST for Python and regex for Rust. No API call, no network, ~500ms (down from ~70s).
2. Compares extracted contracts against the prior generation's contracts
3. Scores drift on a 0.0–1.0 scale (weights: removals 0.4, modifications 0.35, additions 0.25)
4. Applies penalties for assumption drift and dependency drift
5. **PASS** if drift < 0.5 — **BLOCK** if drift >= 0.5

**What a contract tracks:**
- Interfaces, classes, functions and their fields and methods
- Which components consume which contracts
- Implicit assumptions — units, coordinate systems, data formats
- External dependencies

**Two modes:**
- **Solo** (default CI): extracts the working tree, scores against the last committed generation, and persists.
- **Managed** (`CDMAD_MANAGED=1`, set by the Conductor): read-only — compares the already-committed session N against N-1, no extraction, no writes.

**Persistence:** Contracts live in `.cdmad/contracts/` (JSON store) or `.cdmad/vdb/{repo}/` (VDB store), checkpoints in `.cdmad/checkpoints/`, session state in `.cdmad/session.json`. This memory does not reset between sessions. It does not reset when the context window fills. It persists until explicitly archived.

**Extraction-agnostic:** The `LLMClient` interface supports swappable extractors.
Default is `SchemaCaptureClient` (static, no network). `AnthropicClient` and the
OpenAI-compatible `HttpxClient` remain as opt-in re-enable paths — swap without
touching the gate logic.

**Storage-agnostic:** The `ContractStore` interface is abstract and swappable.
`ContractStore` is local JSON (`.cdmad/`); `VectorContractStore` is a repo- and
session-scoped JSON VDB (`CDMAD_STORE=vdb`) and the drop-in point for ChromaDB,
Pinecone, or any vector store — one implementation swap, no rewiring required.

---

## Stress Gate

The stress gate validates that generated API code survives real-world load. It spins up a mock API emulator and runs three phases:

| Phase | Load | Pass Criteria |
|-------|------|---------------|
| **NORMAL** | 70% of rate limit | Error rate < 1% |
| **THRESHOLD** | 100% of rate limit | Error rate < 5%, no cascade failures |
| **BREACH** | 120% of rate limit | No retry storm, recovery within 30s |

Measures: error rate, timeout rate, latency (p50/p95/p99), retry storm detection, cascade failure detection, and recovery time. Code that collapses under load does not pass.

---

## Conductor — Multi-Agent Orchestration

The Conductor is the first and last authority over a multi-agent generation session. Where the gate chain governs individual commits, the Conductor makes the **session** ungameable: nothing reaches the repo until every agent's output is internally consistent.

`python3 -m conductor` opens a setup wizard — number of agents (1–12), each agent's subsystem path and prompt — then runs the session:

1. **Primes the VDB** from the existing codebase on cold start, and writes the dependency graph and expected-interface baseline.
2. **Distributes** each agent to its own staging directory under `/tmp/conductor_staging/`, stamping a `module_key` into its schema. Agents write only to staging — never the repo.
3. **Proofs** output in dependency order. The `PeerChecker` cross-checks each agent against its peers' live schemas for symbol collisions, interface mismatches, and assumption clashes. On conflict it writes an escalating `.conflict_report.txt`; the agent rewrites and re-signals with `.done`. No retry limit.
4. **Atomic commit** once every agent is clean: the contract corpus is committed to the VDB, staged files are copied into the worktree, and a single commit-queue entry is enqueued — one atomic commit for the whole generation. The gate chain then runs as normal, with the memory gate in managed mode reading the just-committed VDB.

The Conductor is pre-gate infrastructure. The gates don't know it exists — they verify what it produces. The merge token still belongs to the orchestrator.

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

Create a `.env` file in the project root:

```bash
CDMAD_LOCAL_DEV=1              # Local dev bypass — disables auth token requirement
CDMAD_DRIFT_THRESHOLD=0.5      # Memory gate drift sensitivity (default 0.5)
# ANTHROPIC_API_KEY is no longer required — the memory gate uses static extraction.
# Set it only if you re-enable AnthropicClient in memory/llm_client.py.
```

`.env` is loaded automatically by `start_gates.py` (invoked by `start_all.sh`) before any service starts. It is gitignored and never committed. See `.env.example` for a full reference.

### 3. Start all services

```bash
./start_all.sh --no-conductor
```

This brings up the seven gate services, the orchestrator, and the commit queue worker in dependency order, with your `.env` variables already in their environment. Leave it running — these are live services. (`./start_all.sh` without the flag also launches the interactive Conductor wizard; `./start_all.sh --stop` halts everything.)

### 4. Install the pre-commit hook

```bash
cp hooks/pre-commit .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
```

Run this once per repository you want governed.

### 5. Commit as normal

```bash
git add .
git commit -m "feat(gates): your message"
```
### 6. No verify commit

When committing documentation changes

```bash
git commit --no-verify -m "feat(sentinel): initial build — sentinel-core, sentinel-types, sentinel-signals, sentinel-controls, sentinel-py"
```

The gate chain runs automatically on every commit. If any gate fails the commit is blocked with the specific failure output. Fix the issue and recommit.

---

## v1.1 — The Agent Navigates the Gate Chain

The system shipped faster because the agent autonomously navigated the gate chain — fixing, retrying, and landing the commit without me in the loop.

### Session Comparison

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

### What Changed

**`dev_loop.sh` closes the human-as-message-bus gap.** The dev loop automates the fix-retry cycle: `git commit` → gate failure → pipe output to `claude -p` → fix → retry. I become the initiator, not the relay.

**`.env` removes key management friction.** `start_gates.py` reads `.env` before spawning any process. The key is set once and persists across restarts.

### Final Gate Run — v1.1

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

The constraints were mine. The gate chain was my design. The merge token is mine to issue. Claude Code handled execution — but every boundary it operated within was set deliberately by me. That's not automation replacing oversight. That's oversight getting better infrastructure.

---

## Operating the System

### Starting and stopping services

```bash
# Start the gate chain + commit queue worker (background)
./start_all.sh --no-conductor

# Start everything, including the interactive Conductor wizard
./start_all.sh

# Stop all services
./start_all.sh --stop
```

### Checking gate health

```bash
curl http://localhost:8000/status
```

Returns the status of all 7 gate services. All must be `ready` for the chain to run.

### Running the gate chain manually

```bash
curl -X POST http://localhost:8000/commit \
  -H "Content-Type: application/json" \
  -d '{
    "agent_id": "claude-code",
    "branch": "main",
    "commit_sha": "abc123def456"
  }'
```

### Viewing results for a commit

```bash
curl http://localhost:8000/results/YOUR_COMMIT_SHA
```

Returns gate-by-gate results, pass/fail status, duration, and failure output for any previously run commit.

### Verifying a merge token

```bash
curl http://localhost:8000/verify/YOUR_TOKEN_HERE
```

Tokens are SHA256-based, timestamped, and stored in SQLite. Each token is single-use and tied to a specific commit SHA.

### Running a single gate independently

Each gate exposes its own endpoints:

```bash
# Check a single gate
curl http://localhost:8001/health   # lint gate

# Run a single gate
curl -X POST http://localhost:8001/run
```

Replace `8001` with the port of any gate (8001–8007).

### Interactive API documentation

```
http://localhost:8000/docs
```

Full Swagger UI — explore and test all orchestrator endpoints interactively.

---

## Interpreting Gate Output

### Pass

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  GATE RESULTS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ✓  lint         PASS   421ms
  ✓  typecheck    PASS   834ms
  ✓  security     PASS   612ms
  ✓  memory       PASS   1203ms
  ✓  test         PASS   4821ms
  ✓  stress       PASS   8432ms
  ✓  build        PASS   203ms
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✦  ALL GATES PASSED
   Merge token: a3f9c2b1d4e5
```

### Fail

```
🚫  COMMIT BLOCKED at gate: MEMORY

    Drift score: 0.72
    Violated contract: FlightData.altitude_meters
    Your assumption: altitude in feet
    Prior contract: altitude is always meters (3 downstream components depend on this)
    
  Fix the issues above and recommit.
```

The failure output identifies the exact gate, the exact violation, and in the case of the memory gate, the exact contract that was broken and which downstream components depend on it.

---

## Adding a Custom Gate

### CLI wrapper gate (wraps an existing tool)

```python
# gates/gates.py — GATE_REGISTRY
"my_gate": {
    "command": ["my-tool", "--check", "."],
    "port": 8008,
    "phase": "parallel",   # or "sequential"
    "description": "My custom check"
},
```

### Custom logic gate (FastAPI app)

```python
"my_gate": {
    "command": [],
    "port": 8008,
    "phase": "parallel",
    "description": "My custom check",
    "custom_app": "my_module.app:app",
},
```

### Custom gate class (extends BaseGate)

```python
"my_gate": {
    "command": [],
    "port": 8008,
    "phase": "sequential",
    "order": 4,   # position in sequential phase
    "description": "My custom check",
    "gate_class": "MyGate",
},
```

Restart after any registry change:

```bash
python3 scripts/start_gates.py --stop && python3 scripts/start_gates.py
```

---

## Development

```bash
# Run all tests
pytest

# Lint
ruff check .

# Type check
mypy . --ignore-missing-imports

# Security audit
bandit -r . -ll -q

# Coverage report
pytest --cov=gates --cov=orchestrator --cov=memory --cov-report=term-missing
```

---

## Project Structure

```
ci-gate-wrapper/
├── start_all.sh              One-command launcher — gate chain + queue worker + conductor
├── conductor/                Multi-agent orchestration with atomic commit
│   ├── cli.py                Setup wizard + session run loop
│   ├── session.py            ConductorSession / AgentSpec
│   ├── vdb_io.py             VDB priming, index reads, session writes
│   ├── distributor.py        Staging layout, module_key stamp, .done protocol
│   ├── prover.py             Dependency-ordered proofing + PeerChecker
│   └── commit.py             Atomic commit → copy to worktree → single queue entry
├── gates/
│   ├── base_gate.py          BaseGate — wraps CLI tool as FastAPI microservice
│   ├── gates.py              GATE_REGISTRY — all 7 gates defined here
│   └── stress_gate.py        StressGate — three-phase load simulation
├── memory/
│   ├── models.py             Contract, drift, checkpoint, session models
│   ├── llm_client.py         LLMClient interface + Anthropic/httpx (lazy, opt-in)
│   ├── schema_capture.py     SchemaCaptureClient — static AST/schema extractor (default)
│   ├── contract_store.py     BaseContractStore + ContractStore (JSON) + VectorContractStore (VDB)
│   ├── peer_checker.py       PeerChecker — intra-session conflict detection
│   ├── drift_scorer.py       Contract comparison and drift scoring (0.0-1.0)
│   ├── memory_gate.py        Core logic — extract/read, compare, score, pass/block
│   └── app.py                FastAPI app on port 8006
├── orchestrator/
│   ├── orchestrator.py       Two-phase execution, SQLite audit trail, merge tokens
│   ├── auth.py               API key authentication, SHA-256 hashing, local dev bypass
│   └── certs.py              Self-signed certificate generation for HTTPS mode
├── queue/
│   └── commit_queue.py       Multi-agent commit queue — serializes commits, queue tokens
├── hooks/
│   ├── pre-commit            Git hook — the agent boundary
│   └── pre_commit.py         Hook logic (importable for testing)
├── scripts/
│   ├── start_gates.py        Service launcher, stopper, and .env loader
│   ├── prime_vdb.py          Standalone VDB primer
│   └── migrate_contracts.py  JSON contract store → VDB migration
├── tests/                    Mirrors module structure — 250 tests
├── .cdmad/                   Runtime — contracts, VDB, checkpoints, sessions (not checked in)
├── .env                      Local environment config — dev flags, optional keys (not checked in)
├── .env.example              Reference template for .env
├── pyproject.toml            Tool configs — ruff, mypy, pytest, bandit
└── requirements.txt          Dependencies
```

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | No | — | No longer required by the memory gate (static extraction). Only needed if `AnthropicClient` is re-enabled in `memory/llm_client.py` |
| `CI_AGENT_ID` | No | `local-dev` | Agent identifier in gate logs and audit trail |
| `CDMAD_LOCAL_DEV` | No | `0` | Set to `1` to bypass auth token requirement in local dev |
| `CDMAD_DRIFT_THRESHOLD` | No | `0.5` | Memory gate drift sensitivity — raise to allow more architectural change per commit |
| `CDMAD_STORE` | No | `json` | Contract store backend — `json` (ContractStore) or `vdb` (VectorContractStore) |
| `CDMAD_VDB_PATH` | No | `.cdmad/vdb` | Root directory for the VDB JSON store |
| `CDMAD_REPO_NAME` | No | auto (git) | Override the repo name used to scope contracts |
| `CDMAD_SCHEMA_PATH` | No | `.cdmad/session_schema.json` | Where the agent writes its declared schema |
| `CDMAD_AGENT_ID` | No | auto (host+pid) | Identifies an agent's live schema file in the VDB |
| `CDMAD_MANAGED` | No | `0` | Set to `1` (by the Conductor) for read-only managed mode — compares committed N vs N-1 |
| `CDMAD_PEER_CHECK` | No | `1` | Set to `0` to disable intra-session peer checking |
| `CDMAD_RETRIEVAL_TOP_K` | No | `20` | Max contracts retrieved per commit (future ChromaDB) |
| `CDMAD_HTTPS` | No | `0` | Set to `1` to enable HTTPS — self-signed cert generated on first startup |
| `CDMAD_HTTPS_VERIFY` | No | `1` | Set to `0` to skip SSL verification for self-signed certs in the pre-commit hook |
| `CDMAD_MEMORY_CACHE` | No | `0` | Set to `1` to cache memory gate results — skips extraction if source files unchanged |

All variables can be set in `.env` at the project root. See `.env.example` for a reference template.

---

## CDMAD Principles

1. **Generation is optional. Verification is not.** The gate chain is the sole authority on whether code is acceptable.
2. **Never self-certify.** An agent's assessment of its own output is not a gate result.
3. **Declare assumptions before generating.** Implicit assumptions become drift violations.
4. **The merge token is the only valid exit condition.** A work session ends when the orchestrator says it ends.
5. **Constraints are not limitations. They are the architecture.**

---

*CDMAD — Constraint Driven Model Assisted Development*
*Copyright (c) 2026 Brandon Green. All rights reserved.*
