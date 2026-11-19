#!/bin/bash
# Scores the submission with the SusVibes evaluation logic (see sv_eval.py).
# Runs in a fresh container of the task image; the agent's `git diff` arrives
# at /logs/artifacts/model.patch.
set -uo pipefail

mkdir -p /logs/verifier

PYTHON_BIN="$(command -v python3 || command -v python || true)"
if [ -z "${PYTHON_BIN}" ]; then
  echo "No python interpreter found in the task image" >&2
  echo 0 > /logs/verifier/reward.txt
  exit 1
fi

# -I keeps the task's PYTHONPATH and working directory out of the grader's own
# imports; the test command it launches still inherits the full environment.
"${PYTHON_BIN}" -I /tests/sv_eval.py
status=$?

if [ ! -s /logs/verifier/reward.txt ]; then
  echo 0 > /logs/verifier/reward.txt
fi
exit "${status}"
