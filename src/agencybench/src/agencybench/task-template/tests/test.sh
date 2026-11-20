#!/usr/bin/env bash
set -euo pipefail

mkdir -p /logs/verifier

python3 /tests/run_verifier.py \
  --runner-family "{runner_family}" \
  --eval-script /tests/upstream/eval_task.py \
  --description /tests/upstream/description.json \
  --workspace /workspace \
  --stages-dir /tmp/.agencybench_stages \
  --reward-txt /logs/verifier/reward.txt \
  --reward-json /logs/verifier/reward.json \
  --report-json /logs/verifier/agencybench_report.json
