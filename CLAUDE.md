# CI Gate Wrapper — CLAUDE.md

## What This Project Is

CDMAE (Constraint Driven Machine Assisted Engineering) enforcement layer. A gate chain that sits between code generation and merge. Every commit must pass lint, typecheck, security, test, and build gates. No merge token = no merge.

## Architecture

- **Gates** (`gates/`): FastAPI microservices wrapping CLI tools. Each gate runs on its own port (8001-8005).
- **Orchestrator** (`orchestrator/`): Coordinates gate execution on port 8000. Phase 1 (parallel): lint, typecheck, security. Phase 2 (sequential): test, build. Issues merge tokens on full pass.
- **Hooks** (`hooks/`): Git pre-commit hook calls orchestrator, blocks commit without merge token.
- **Scripts** (`scripts/`): Service lifecycle — `start_gates.py` launches all services.
- **Tests** (`tests/`): Mirrors module structure. Run with `pytest`.

## Development Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run all tests
pytest

# Run linter
ruff check .

# Run type checker
mypy . --ignore-missing-imports

# Run security audit
bandit -r . -ll -q

# Start all gate services
python scripts/start_gates.py

# Stop all gate services
python scripts/start_gates.py --stop
```

## Conventions

- **Python 3.12+** required
- **Type annotations** on all function signatures — no implicit `Any`
- **Tests** for every new function — live in `tests/` mirroring module structure
- **Commit messages**: `type(scope): description` (e.g., `feat(gates): add custom lint rule`)
- **No hardcoded secrets**, no `eval()`, no `shell=True` in subprocess
- Config lives in `pyproject.toml` (ruff, mypy, pytest, bandit)

## CDMAE Rules

1. Never self-certify code as correct — the gate chain is the sole verification authority
2. Declare assumptions before generating non-trivial code
3. Flag constraint violations immediately — do not silently comply
4. Run checkpoint before every file write
5. A merge token from the orchestrator is the only valid exit condition for a work session

## Key Files

| File | Purpose |
|------|---------|
| `gates/base_gate.py` | BaseGate class — wraps CLI tool as FastAPI service |
| `gates/gates.py` | GATE_REGISTRY — defines all 5 gates |
| `orchestrator/orchestrator.py` | Orchestrator — two-phase execution, SQLite, tokens |
| `hooks/pre-commit` | Git hook — calls orchestrator on commit |
| `hooks/pre_commit.py` | Importable copy of pre-commit hook (for testing) |
| `scripts/start_gates.py` | Service launcher/stopper |
| `pyproject.toml` | All tool configs (ruff, mypy, pytest, bandit) |
