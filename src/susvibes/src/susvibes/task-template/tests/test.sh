#!/bin/bash
# Scores the submission with the SusVibes evaluation logic (see sv_eval.py).
# Runs in a fresh container of the task image; the agent's `git diff` arrives
# at /logs/artifacts/model.patch.
set -uo pipefail

mkdir -p /logs/verifier
# Only this run decides the reward.
rm -f /logs/verifier/reward.txt /logs/verifier/reward.json

PYTHON_BIN="$(command -v python3 || command -v python || true)"
if [ -z "${PYTHON_BIN}" ]; then
  echo "No python interpreter found in the task image" >&2
  exit 1
fi

# -I keeps the task's PYTHONPATH and working directory out of the grader's own
# imports; the test command it launches still inherits the full environment.
# sv_eval.py writes the reward whenever it reaches a verdict. If the evaluation
# itself breaks it writes none and exits non-zero, so the trial shows up as an
# error instead of a failed submission.
"${PYTHON_BIN}" -I /tests/sv_eval.py
