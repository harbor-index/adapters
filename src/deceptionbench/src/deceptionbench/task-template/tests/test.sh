#!/bin/bash
set -euo pipefail
mkdir -p /logs/verifier
# verify.py writes /logs/verifier/reward.txt only for valid scores/submissions.
exec python -I /tests/verify.py
