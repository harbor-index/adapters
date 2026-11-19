#!/bin/bash
set -uo pipefail

PREDICTION=/mnt/output/prediction.csv
CONFIG=$(ls /tests/configs/task_*.json)
TASK_ID=$(basename "$CONFIG" .json)
STAGE=$(mktemp -d)
mkdir -p "$STAGE/predictions/$TASK_ID" /logs/verifier

# The gold file is uploaded under /tests at verification time, so a symlink
# written by the agent could otherwise resolve to it.
if [ -L "$PREDICTION" ]; then
  echo "Rejecting symlinked prediction: $PREDICTION" >&2
elif [ -f "$PREDICTION" ]; then
  cp "$PREDICTION" "$STAGE/predictions/$TASK_ID/prediction.csv"
else
  echo "No prediction found at $PREDICTION" >&2
fi

python3 /tests/evaluate.py \
  --prediction-root "$STAGE/predictions" \
  --gold-root /tests/gold \
  --config-root /tests/configs \
  --output /logs/verifier/evaluation_summary.json || exit $?

python3 - <<'EOF'
import json

with open("/logs/verifier/evaluation_summary.json", encoding="utf-8") as handle:
    summary = json.load(handle)
print(json.dumps(summary["tasks"][0], ensure_ascii=False, indent=2))
with open("/logs/verifier/reward.txt", "w", encoding="utf-8") as handle:
    handle.write(f"{summary['passed_task_count']}\n")
EOF
