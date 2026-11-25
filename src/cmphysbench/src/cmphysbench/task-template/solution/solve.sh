#!/bin/bash
set -euo pipefail

# Reference answer from the dataset, written inside \boxed{} as the graded format requires.
mkdir -p /app
cat > /app/answer.txt <<'__CMPHYSBENCH_REFERENCE_ANSWER__'
\boxed{{answer}}
__CMPHYSBENCH_REFERENCE_ANSWER__
