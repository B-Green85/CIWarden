#!/usr/bin/env python3
"""
Start all CIWarden gate services + orchestrator in separate processes.
Usage: python scripts/start_gates.py
       python scripts/start_gates.py --stop
"""
import json
import os
import signal
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PID_FILE = os.path.join(ROOT, ".gate_pids.json")
ENV_FILE = os.path.join(ROOT, ".env")

sys.path.insert(0, ROOT)
from gates.gates import GATE_REGISTRY  # noqa: E402


def load_env(path: str | None = None) -> dict[str, str]:
    """Load variables from a .env file into os.environ.

    Returns the dict of variables that were loaded.
    Skips blank lines, comments (#), and lines without '='.
    Values may be optionally quoted with single or double quotes.
    """
    env_path = path or ENV_FILE
    loaded: dict[str, str] = {}

    if not os.path.isfile(env_path):
        return loaded

    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Strip matching quotes
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            os.environ[key] = value
            loaded[key] = value

    return loaded


def start_all() -> None:
    loaded = load_env()
    if loaded:
        print(f"\n[ENV] Loaded {len(loaded)} variable(s) from .env")

    pids: dict[str, int] = {}
    print("\n[CIWARDEN] Starting all services...\n")

    # Start each gate service
    for name, config in GATE_REGISTRY.items():
        proc = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "gates", "gates.py"), name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        pids[name] = proc.pid
        print(f"  ● {name:<12} port {config['port']}  PID {proc.pid}")
        time.sleep(0.2)

    # Start orchestrator
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "orchestrator", "orchestrator.py")],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    pids["orchestrator"] = proc.pid
    print(f"  ● {'orchestrator':<12} port 8000      PID {proc.pid}")

    # Save PIDs
    with open(PID_FILE, "w") as f:
        json.dump(pids, f)

    time.sleep(2)
    print("\n[READY] Orchestrator: http://localhost:8000")
    print("        Status:       http://localhost:8000/status")
    print("        Docs:         http://localhost:8000/docs\n")
    print("[INFO]  Install hook: cp hooks/pre-commit .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit\n")

    # Keep alive — forward orchestrator output
    try:
        if proc.stdout is not None:
            for line in proc.stdout:
                print(line.decode(), end="")
    except KeyboardInterrupt:
        print("\n[SHUTDOWN] Stopping all gate services...")
        stop_all()


def stop_all() -> None:
    if not os.path.exists(PID_FILE):
        print("No running gate services found.")
        return

    with open(PID_FILE) as f:
        pids: dict[str, int] = json.load(f)

    for name, pid in pids.items():
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"  ✓ Stopped {name} (PID {pid})")
        except ProcessLookupError:
            print(f"  - {name} already stopped")

    os.remove(PID_FILE)
    print("\n[DONE] All services stopped.\n")


if __name__ == "__main__":
    if "--stop" in sys.argv:
        stop_all()
    else:
        start_all()
