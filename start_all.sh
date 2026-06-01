#!/usr/bin/env bash
#
# start_all.sh — bring up the GateChain stack in dependency order:
#
#   1. Gate chain        orchestrator :8000 + gates 8001-8007   [background]
#   2. Commit queue      drains commits serially, calls :8000    [background]
#   3. Conductor         interactive setup wizard                [foreground]
#
# Order matters: the queue worker posts to the orchestrator (:8000/commit), and
# the conductor's atomic commit enqueues into the worker. So gates must be up
# before the worker, and both before the conductor.
#
# Usage:
#   ./start_all.sh                 start gates + queue, then launch the conductor wizard
#   ./start_all.sh --no-conductor  start only the two background services, then exit
#   ./start_all.sh --stop          stop the background services (gates + queue worker)
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PY="${PYTHON:-python3}"
RUN_DIR="$ROOT/.run"
QUEUE_PID_FILE="$RUN_DIR/queue_worker.pid"
STATUS_URL="http://localhost:8000/status"

mkdir -p "$RUN_DIR"

stop_all() {
  if [[ -f "$QUEUE_PID_FILE" ]]; then
    echo "[start_all] stopping commit queue worker (PID $(cat "$QUEUE_PID_FILE"))..."
    kill "$(cat "$QUEUE_PID_FILE")" 2>/dev/null || true
    rm -f "$QUEUE_PID_FILE"
  fi
  echo "[start_all] stopping gate chain..."
  "$PY" "$ROOT/scripts/start_gates.py" --stop || true
}

case "${1:-}" in
  --stop)
    stop_all
    exit 0
    ;;
esac

# ── 1. Gate chain ────────────────────────────────────────────────────────────
# Idempotent: if the orchestrator already answers on :8000, reuse it rather than
# launching a second stack (which would fail with "address already in use").
if curl -sf -o /dev/null "$STATUS_URL"; then
  echo "[start_all] 1/3 gate chain already up on :8000 — reusing."
else
  # start_gates.py detaches the gate processes itself, then blocks forwarding
  # orchestrator output — so run the supervisor in the background.
  echo "[start_all] 1/3 starting gate chain (orchestrator :8000 + gates)..."
  "$PY" "$ROOT/scripts/start_gates.py" >"$RUN_DIR/gates.log" 2>&1 &

  echo "[start_all]      waiting for orchestrator on :8000..."
  for i in $(seq 1 60); do
    if curl -sf -o /dev/null "$STATUS_URL"; then
      echo "[start_all]      orchestrator is up."
      break
    fi
    if [[ "$i" -eq 60 ]]; then
      echo "[start_all] ERROR: orchestrator did not come up — see $RUN_DIR/gates.log" >&2
      stop_all
      exit 1
    fi
    sleep 0.5
  done
fi

# ── 2. Commit queue worker ───────────────────────────────────────────────────
echo "[start_all] 2/3 starting commit queue worker (repo: $ROOT)..."
"$PY" "$ROOT/queue/commit_queue.py" worker --repo "$ROOT" >"$RUN_DIR/queue_worker.log" 2>&1 &
echo $! >"$QUEUE_PID_FILE"
echo "[start_all]      queue worker PID $(cat "$QUEUE_PID_FILE")  (log: $RUN_DIR/queue_worker.log)"

# ── 3. Conductor ─────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--no-conductor" ]]; then
  echo "[start_all] background services up. Skipping conductor (--no-conductor)."
  echo "[start_all] stop them with: ./start_all.sh --stop"
  exit 0
fi

echo "[start_all] 3/3 launching conductor wizard..."
echo "[start_all]      (gate chain + queue worker stay up in the background;"
echo "[start_all]       run ./start_all.sh --stop to halt them afterwards)"
echo
exec "$PY" -m conductor
