#!/usr/bin/env bash
set -euo pipefail
mkdir -p /logs/verifier
# Missing or failed simulator evaluations must not leave a stale reward.
rm -f /logs/verifier/reward.txt /logs/verifier/reward.json
source /root/miniconda3/etc/profile.d/conda.sh
conda activate RoboDojo
export PYTHONPATH="/tests${PYTHONPATH:+:$PYTHONPATH}"
python -m robodojo.runtime \
  --spec /tests/task_spec.json \
  --submission /workspace/submission.json \
  --root /workspace/RoboDojo \
  --output-dir /logs/verifier
