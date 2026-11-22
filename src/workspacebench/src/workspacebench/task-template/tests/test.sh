#!/bin/bash
# Collect the agent's outputs and grade them with upstream's agent-as-a-judge.
# grade.py writes /logs/verifier/reward.txt (rubrics passed / total). If the
# judge produces no result at all, no reward is written and the trial errors.
set -uo pipefail

rm -f /logs/verifier/reward.txt /logs/verifier/reward.json
python3 /tests/grade.py
