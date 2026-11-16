#!/bin/bash
# Grade /workspace/solution.xlsx with the official MBABench 2.0 judge and write
# the reward (judge total / 100) to /logs/verifier/reward.txt. A grading that
# cannot be completed exits non-zero without a reward, so infrastructure or
# judge failures surface as verifier errors rather than as a score of 0.
set -euo pipefail

mkdir -p /logs/verifier
exec python3 /tests/mbabench_verifier.py
