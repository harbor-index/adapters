#!/bin/bash
# Grades /workspace/answer.json with the upstream CaveBench scorer
# (contracts.score, vendored verbatim). Writes /logs/verifier/reward.json:
#   reward          1 if every event's control set matches the reference (exact match)
#   event_accuracy  fraction of events whose control set matches
#   format_valid    1 if the answer parses as one legal "operations" object
set -uo pipefail

mkdir -p /logs/verifier
python3 /tests/grade.py \
    --answer /workspace/answer.json \
    --expected /tests/expected.json \
    --out /logs/verifier
