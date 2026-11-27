#!/bin/bash
# Grade the files the agent saved in /workspace against the gold files with
# upstream's metric functions (evaluate.py writes /logs/verifier/reward.txt).
# A failing check scores 0 inside evaluate.py, as upstream does; if the grader
# itself cannot run, no reward is written and the trial errors instead.
# Runs in a fresh container from the task image, which holds only the agent's
# output files (task.toml artifacts). The verifier venv is built into the image,
# separate from the agent's packages.
set -uo pipefail

mkdir -p /logs/verifier
rm -f /logs/verifier/reward.txt
/opt/adb-verifier/bin/python /tests/evaluate.py
