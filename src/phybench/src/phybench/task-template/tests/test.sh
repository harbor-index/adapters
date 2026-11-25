#!/bin/bash
set -uo pipefail

mkdir -p /logs/verifier
# Never trust a reward file left over from an earlier phase.
rm -f /logs/verifier/reward.txt /logs/verifier/reward.json

# Absolute interpreter path and isolated mode (-I): ignores PATH, PYTHONPATH
# and user site-packages. score.py writes /logs/verifier/reward.json (all
# metrics) and /logs/verifier/reward.txt (primary reward). If scoring itself
# crashes, no reward is written: an infrastructure failure, not a score of 0.
/usr/local/bin/python3 -I /tests/score.py
status=$?

if [ "$status" -ne 0 ] || [ ! -s /logs/verifier/reward.json ]; then
  echo "Scoring failed (exit $status); no reward written." >&2
  exit 1
fi
exit 0
