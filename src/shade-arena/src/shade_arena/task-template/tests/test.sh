#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH="/opt/shade${PYTHONPATH:+:$PYTHONPATH}"
# The verifier writes /logs/verifier/reward.txt and the per-metric reward.json.
python /tests/verify.py
