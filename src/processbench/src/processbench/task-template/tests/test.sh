#!/bin/bash
# Writes 1 to /logs/verifier/reward.txt if the predicted earliest-error index
# matches the label, else 0. Per-task details go to /logs/verifier/metrics.json
# for aggregate_metrics.py (ProcessBench reports F1 per subset, not per task).

mkdir -p /logs/verifier

if ! python3 /tests/grade.py; then
  echo "grader failed; writing reward 0"
  echo 0 > /logs/verifier/reward.txt
fi

exit 0
