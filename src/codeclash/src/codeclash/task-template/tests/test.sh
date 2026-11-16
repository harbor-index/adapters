#!/usr/bin/env bash
set -euo pipefail
mkdir -p /logs/verifier
rm -f /logs/verifier/reward.txt /logs/verifier/reward.json
python /tests/evidence.py --evidence /codeclash/evidence --task-config /codeclash/task-config.json --output-dir /logs/verifier
