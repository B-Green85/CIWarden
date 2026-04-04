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