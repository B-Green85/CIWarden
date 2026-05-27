#!/usr/bin/env bash
# dev_loop.sh — CDMAE auto-fix commit loop
# Usage: scripts/dev_loop.sh "commit message" [max_retries]
set -euo pipefail

COMMIT_MSG="${1:?Usage: scripts/dev_loop.sh \"commit message\" [max_retries]}"
MAX_RETRIES="${2:-10}"
ATTEMPT=0

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  CDMAE Dev Loop — auto-fix commit cycle"
echo "  Max retries: ${MAX_RETRIES}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

while [ "$ATTEMPT" -lt "$MAX_RETRIES" ]; do
    ATTEMPT=$((ATTEMPT + 1))
    echo ""
    echo "[attempt ${ATTEMPT}/${MAX_RETRIES}] Staging and committing..."

    git add .
    COMMIT_OUTPUT=$(git commit -m "${COMMIT_MSG}" 2>&1) && {
        # Commit succeeded — extract merge token from output
        echo "$COMMIT_OUTPUT"
        TOKEN=$(echo "$COMMIT_OUTPUT" | grep -oE 'Merge token: [0-9a-f]+' | awk '{print $3}')
        echo ""
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "  ALL GATES PASSED on attempt ${ATTEMPT}"
        if [ -n "$TOKEN" ]; then
            echo "  Merge token: ${TOKEN}"
        fi
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        exit 0
    }

    # Commit was blocked by a gate
    echo "$COMMIT_OUTPUT"
    echo ""
    echo "[attempt ${ATTEMPT}/${MAX_RETRIES}] Gate failure detected. Invoking Claude to fix..."

    # Truncate output to avoid oversized prompts
    TRUNCATED=$(echo "$COMMIT_OUTPUT" | tail -60)

    claude -p "The CDMAE gate chain blocked this commit. Fix the issues in the codebase. Do not modify tests or gate configs. Here is the gate output:

${TRUNCATED}"

    echo ""
    echo "[attempt ${ATTEMPT}/${MAX_RETRIES}] Claude applied fixes. Retrying..."
done

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  FAILED after ${MAX_RETRIES} attempts"
echo "  Manual intervention required."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
exit 1
