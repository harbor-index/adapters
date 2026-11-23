#!/bin/bash
set -uo pipefail
mkdir -p /logs/verifier
python3 /tests/score.py || echo 0 > /logs/verifier/reward.txt
