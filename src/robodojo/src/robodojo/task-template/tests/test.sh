#!/usr/bin/env bash
set -euo pipefail
mkdir -p /logs/verifier
# A failed or incomplete evaluation must not leave a stale reward.
rm -f /logs/verifier/reward.txt /logs/verifier/reward.json
export PYTHONPATH="/tests${PYTHONPATH:+:$PYTHONPATH}"
python -m robodojo.verifier \
  --spec /tests/task_spec.json \
  --results-dir /harbor-robodojo/results \
  --output-dir /logs/verifier
