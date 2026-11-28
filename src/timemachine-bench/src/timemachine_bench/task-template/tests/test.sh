#!/bin/bash
# Grades the submission in a fresh container of the task image (separate
# verifier), as upstream runs its tests in a fresh container of the image with
# the agent's repository mounted. evaluate.py applies the agent's edits to the
# existing non-test files of /work, runs the upstream test script and writes
# reward 1 if it exits with status 0, else 0 (TimeMachine-bench pass@1).
set -uo pipefail

mkdir -p /logs/verifier
rm -f /logs/verifier/reward.txt /logs/verifier/reward.json

# -I keeps the task's environment variables and working directory out of the
# grader's own imports; the test script it launches runs with the image's
# environment, as upstream.
python3 -I /tests/evaluate.py
status=$?

if [ "${status}" -ne 0 ]; then
  # Grading did not finish (for example the submission artifact is missing),
  # so no reward is written and the trial ends as an error, not as a score.
  rm -f /logs/verifier/reward.txt /logs/verifier/reward.json
fi
exit "${status}"
