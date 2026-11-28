#!/bin/bash
# AgentSWE ships no reference agents for Creation tasks, so there is no oracle that can
# reach full reward. This delivers a structurally valid null agent (it writes no output)
# so that an oracle run exercises the whole pipeline: freeze, Candidate job, deterministic
# gates and Result judge. The expected reward is 0.
set -euo pipefail
mkdir -p /workspace/submission
cat > /workspace/submission/run_agent.py <<'PY'
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--input", required=True)
parser.add_argument("--output", required=True)
parser.parse_args()
PY
