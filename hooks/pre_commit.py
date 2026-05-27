#!/usr/bin/env python3
"""
CI Gate pre-commit hook.
Install: cp hooks/pre-commit .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
"""
import os
import subprocess
import sys

try:
    import httpx
except ImportError:
    print("[CI GATE] httpx not installed — run: pip install httpx")
    sys.exit(1)

AGENT_ID = "local-dev"  # override with env var CI_AGENT_ID


def _orchestrator_url() -> str:
    scheme = "https" if os.environ.get("CDMAE_HTTPS", "0") == "1" else "http"
    return f"{scheme}://localhost:8000/commit"

def get_current_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL
        ).decode().strip()
    except subprocess.CalledProcessError:
        # Initial commit — HEAD doesn't exist yet. Use the staged tree hash.
        return subprocess.check_output(
            ["git", "write-tree"], stderr=subprocess.DEVNULL
        ).decode().strip()

def get_current_branch() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"]
        ).decode().strip()
    except Exception:
        return "unknown"

def main() -> None:
    agent_id = os.environ.get("CI_AGENT_ID", AGENT_ID)
    sha = get_current_sha()
    branch = get_current_branch()

    print(f"\n{'━'*50}")
    print("  CI GATE WRAPPER — CDMAE Enforcement Layer")
    print(f"{'━'*50}")
    print(f"  Agent:  {agent_id}")
    print(f"  Branch: {branch}")
    print(f"  SHA:    {sha[:12]}...")
    print(f"{'━'*50}\n")

    headers: dict[str, str] = {}
    gate_token = os.environ.get("CDMAE_GATE_TOKEN")
    if gate_token:
        headers["X-Gate-Token"] = gate_token
    else:
        # Local dev bypass — tell orchestrator to skip auth
        os.environ["CDMAE_LOCAL_DEV"] = "1"

    url = _orchestrator_url()
    verify_ssl = os.environ.get("CDMAE_HTTPS_VERIFY", "1") != "0"

    try:
        response = httpx.post(url, json={
            "agent_id": agent_id,
            "branch": branch,
            "commit_sha": sha
        }, headers=headers, timeout=300, verify=verify_ssl)

        data = response.json()

    except httpx.ConnectError:
        print("⚠  Orchestrator not running on localhost:8000")
        print("   Start it with: python orchestrator/orchestrator.py")
        print("   Or bypass with: git commit --no-verify (not recommended)\n")
        sys.exit(1)

    # Print results
    print(f"\n{'━'*50}")
    print("  GATE RESULTS")
    print(f"{'━'*50}")

    for gate_name, result in data.get("results", {}).items():
        icon = "✓" if result["status"] == "pass" else "✗"
        ms = result.get("duration_ms", 0)
        print(f"  {icon}  {gate_name:<12} {result['status'].upper():<6} {ms}ms")

    print(f"{'━'*50}")
    print(f"  Total: {data['total_duration_ms']}ms")
    print(f"{'━'*50}\n")

    if data["merge_token"] is None:
        blocked = data.get("blocked_at", "unknown")
        reason = data.get("reason", "")
        print(f"🚫  COMMIT BLOCKED at gate: {blocked.upper()}\n")
        if reason:
            # Print first 10 lines of failure reason
            lines = reason.strip().split("\n")[:10]
            for line in lines:
                print(f"    {line}")
            if len(reason.strip().split("\n")) > 10:
                print(f"    ... ({len(reason.strip().split(chr(10))) - 10} more lines)")
        print("\n  Fix the issues above and recommit.\n")
        sys.exit(1)

    print("✦  ALL GATES PASSED")
    print(f"   Merge token: {data['merge_token']}\n")
    sys.exit(0)


if __name__ == "__main__":
    main()
