#!/bin/bash
# Grades /app/answer.txt with MedXpertQA's own answer-cleansing and
# exact-match logic (see verify.py) and writes accuracy (0 or 1) as the reward.

mkdir -p /logs/verifier

python3 /tests/verify.py \
    --answer-file /app/answer.txt \
    --ground-truth /tests/ground_truth.json \
    --reward-file /logs/verifier/reward.txt

if [ $? -ne 0 ] || [ ! -s /logs/verifier/reward.txt ]; then
    echo "Verifier failed to produce a reward" >&2
    exit 1
fi

exit 0
