#!/bin/bash
# Grades /app/answer.txt with the official GAIA scorer (question_scorer,
# vendored unchanged apart from one import in gaia_scorer.py next to this file).

set -euo pipefail

echo "=== STARTING GAIA TEST EXECUTION ==="

mkdir -p /logs/verifier
# The reward stays 0 unless the scorer accepts the answer.
echo 0 > /logs/verifier/reward.txt

if [ ! -f /app/answer.txt ]; then
    echo "ERROR: /app/answer.txt not found"
    exit 0
fi

# The scorer receives the file contents unmodified, as the leaderboard receives
# the submitted answer; it ignores whitespace itself. Exit 0 = correct,
# 10 = incorrect, anything else = scorer error.
status=0
python3 -I - <<'PY' || status=$?
import sys

sys.path.insert(0, "/tests")
from gaia_scorer import question_scorer

with open("/app/answer.txt", encoding="utf-8", errors="replace") as f:
    agent_answer = f.read()
with open("/tests/expected_answer.txt", encoding="utf-8") as f:
    expected_answer = f.read()

print(f"Agent answer: {agent_answer!r}")
print(f"Expected answer: {expected_answer!r}")
sys.exit(0 if question_scorer(agent_answer, expected_answer) else 10)
PY

case "$status" in
    0)
        echo "✓ Test passed - answer is correct"
        echo 1 > /logs/verifier/reward.txt
        ;;
    10)
        echo "✗ Test failed - answer is incorrect"
        ;;
    *)
        echo "ERROR: scorer exited with status $status; reward is 0"
        ;;
esac

echo "=== SCRIPT FINISHED ==="
exit 0
