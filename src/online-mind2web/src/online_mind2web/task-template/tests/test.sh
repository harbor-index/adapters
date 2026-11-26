#!/bin/bash
# Online-Mind2Web verifier: score the agent's trajectory with WebJudge.
# Trajectory contract: /app/output/<task_id>/result.json + trajectory/NNNN.png
# (upstream online-mind2web-v2 submission layout).
set -u
mkdir -p /logs/verifier

TASK_ID=$(python3 -c "import json;print(json.load(open('/tests/task.json'))['task_id'])")
TRAJ_DIR="/app/output/${TASK_ID}"

# Keep the judged trajectory with the verifier logs for inspection / parity upload.
if [ -d "$TRAJ_DIR" ]; then
  mkdir -p /logs/verifier/trajectory && cp -r "$TRAJ_DIR"/. /logs/verifier/trajectory/ || true
fi

/opt/webjudge/bin/python /tests/webjudge/run_webjudge.py \
  --task-json /tests/task.json \
  --trajectory-dir "$TRAJ_DIR" \
  --output /logs/verifier/webjudge_result.json \
  --reward-file /logs/verifier/reward.txt
