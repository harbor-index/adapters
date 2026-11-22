#!/bin/bash
# Harbor verifier for one TavernBench task.
# The reward is 1 if the answer in /app/answer.json is correct under the
# benchmark's scoring rule for this task, and 0 otherwise. The graded score and
# the parsed answer are kept in /logs/verifier/score.json.
set -uo pipefail

mkdir -p /logs/verifier

if python3 /tests/score.py \
    --answer /app/answer.json \
    --label /tests/label.json \
    --details /logs/verifier/score.json; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi

exit 0
