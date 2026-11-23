#!/bin/bash
set -euo pipefail

mkdir -p /logs/verifier

python3 -I /tests/grade.py \
  /app/answer.json \
  /tests/labels.json \
  /logs/verifier/reward.txt \
  /logs/verifier/details.json
