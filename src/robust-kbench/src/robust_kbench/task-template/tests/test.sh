#!/bin/bash
set -euo pipefail

mkdir -p /logs/verifier
echo 0 > /logs/verifier/reward.txt

echo "=== Robust-KBench Verifier ==="
if [ ! -f /app/kernel.cu ]; then
    echo "ERROR: /app/kernel.cu does not exist!"
    exit 1
fi

echo "--- Candidate Kernel Preview ---"
head -n 25 /app/kernel.cu
echo "..."

echo "--- Compiling and Validating Robustness ---"
set +e
python3 /tests/eval_verifier.py
EXIT_CODE=$?
set -e

if [ ${EXIT_CODE} -eq 0 ]; then
    echo "=== VERIFICATION PASSED ==="
    echo 1 > /logs/verifier/reward.txt
    exit 0
else
    echo "=== VERIFICATION FAILED ==="
    echo 0 > /logs/verifier/reward.txt
    exit ${EXIT_CODE}
fi
