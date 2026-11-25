#!/bin/bash
set -euo pipefail

# Reference answer from the dataset, written verbatim.
mkdir -p /app
cat > /app/answer.txt <<'__PHYBENCH_REFERENCE_ANSWER__'
{answer}
__PHYBENCH_REFERENCE_ANSWER__
