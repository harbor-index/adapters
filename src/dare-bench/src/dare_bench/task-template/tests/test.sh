#!/bin/bash
set -uo pipefail

mkdir -p /logs/verifier
rm -f /logs/verifier/reward.txt

python3 /tests/verify.py --workspace /workspace --tests /tests --logs /logs/verifier
status=$?

if [ "$status" -ne 0 ]; then
    echo "Verifier infrastructure error (exit $status); no reward written." >&2
    exit "$status"
fi
