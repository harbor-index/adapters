#!/bin/bash
# EXP-Bench verifier: git diff + LLM judge (upstream prompts) + execution check.
# judge.py writes /logs/verifier/reward.json (all metrics) and /logs/verifier/reward.txt
# (the primary reward: conclusion correctness).
# Judge dependencies are pinned to the upstream EXP-Bench environment.yml and installed
# into an isolated venv so they cannot affect the agent's reproduce script.
set -uo pipefail

mkdir -p /logs/verifier

PYTHON_BIN="$(command -v python3 || command -v python)"
"$PYTHON_BIN" -m venv /tmp/exp-bench-judge-venv
/tmp/exp-bench-judge-venv/bin/pip install -q --disable-pip-version-check \
    "openai==1.66.3" "tiktoken==0.9.0" "langchain-text-splitters==0.3.6" \
    || { echo "failed to install judge dependencies" >&2; exit 1; }

/tmp/exp-bench-judge-venv/bin/python /tests/judge.py
