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
         │   └── MEMORY    :8006  anthropic    drift detection    │
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
| **memory** | 8006 | parallel | Anthropic API | Generational memory — extracts architectural contracts, scores drift against prior generations, blocks on drift >= 0.3 |
| **test** | 8004 | sequential | pytest | Unit and integration test suite with coverage enforcement |
| **stress** | 8007 | sequential | load runner | Three-phase API stress testing — NORMAL (70%), THRESHOLD (100%), BREACH (120%) of rate limit |
| **build** | 8005 | sequential | py_compile | Compilation validation — clean compile, no circular imports |

---

## Generational Memory Gate

The memory gate enforces **architectural continuity** across code generations. It prevents silent contract drift — the invisible divergence where field names shift, units change, and interface shapes break downstream consumers without anyone noticing until production.

**How it works:**

1. Extracts architectural contracts from source code via the Anthropic API
2. Compares extracted contracts against the prior generation's contracts
3. Scores drift on a 0.0–1.0 scale (weights: removals 0.4, modifications 0.35, additions 0.25)
4. Applies penalties for assumption drift and dependency drift
5. **PASS** if drift < 0.3 — **BLOCK** if drift >= 0.3

**What a contract tracks:**
- Interfaces, classes, functions and their fields and methods
- Which components consume which contracts
- Implicit assumptions — units, coordinate systems, data formats
- External dependencies

**Persistence:** Contracts live in `.cdmad/contracts/`, checkpoints in `.cdmad/checkpoints/`, session state in `.cdmad/session.json`. This memory does not reset between sessions. It does not reset when the context window fills. It persists until explicitly archived.

**Provider-agnostic:** The `LLMClient` interface supports swappable backends. 
Default is the Anthropic SDK. `HttpxClient` provides an OpenAI-compatible 
stub for any provider — swap the model without touching the gate logic.

**Storage-agnostic:** The `ContractStore` interface is abstract and swappable. 
Default is local JSON (`.cdmad/`). `VectorContractStore` stub exists as the 
drop-in point for ChromaDB, Pinecone, or any vector store — one implementation 
swap, no rewiring required.

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

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Set environment variables

```bash
export ANTHROPIC_API_KEY="sk-ant-..."   # Required for memory gate
export CI_AGENT_ID="your-agent-name"    # Optional — defaults to "local-dev"
```

Add to `~/.zshrc` or `~/.bashrc` to persist across sessions.

### 3. Start all gate services

```bash
python scripts/start_gates.py
```

This starts 7 gate services and the orchestrator. Leave the terminal running — these are live services.

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

The gate chain runs automatically on every commit. If any gate fails the commit is blocked with the specific failure output. Fix the issue and recommit.

---

## Operating the System

### Starting and stopping services

```bash
# Start all services
python scripts/start_gates.py

# Stop all services
python scripts/start_gates.py --stop
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
python scripts/start_gates.py --stop && python scripts/start_gates.py
```

---

## Development

```bash
# Run all 93 tests
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
├── gates/
│   ├── base_gate.py          BaseGate — wraps CLI tool as FastAPI microservice
│   ├── gates.py              GATE_REGISTRY — all 7 gates defined here
│   └── stress_gate.py        StressGate — three-phase load simulation
├── memory/
│   ├── models.py             Contract, drift, checkpoint, session models
│   ├── llm_client.py         LLMClient interface + Anthropic/httpx implementations
│   ├── contract_store.py     BaseContractStore (abstract) + ContractStore 
│   │                         (JSON)+VectorContractStore (VDB stub)
│   ├── drift_scorer.py       Contract comparison and drift scoring (0.0-1.0)
│   ├── memory_gate.py        Core logic — extract, compare, score, pass/block
│   └── app.py                FastAPI app on port 8006
├── orchestrator/
│   └── orchestrator.py       Two-phase execution, SQLite audit trail, merge tokens
├── hooks/
│   ├── pre-commit            Git hook — the agent boundary
│   └── pre_commit.py         Hook logic (importable for testing)
├── scripts/
│   └── start_gates.py        Service launcher and stopper
├── tests/                    Mirrors module structure — 93 tests
├── .cdmad/                   Runtime — contracts, checkpoints, sessions (not checked in)
├── pyproject.toml            Tool configs — ruff, mypy, pytest, bandit
└── requirements.txt          Dependencies
```

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | Yes (memory gate) | — | API key for architectural contract extraction |
| `CI_AGENT_ID` | No | `local-dev` | Agent identifier in gate logs and audit trail |

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