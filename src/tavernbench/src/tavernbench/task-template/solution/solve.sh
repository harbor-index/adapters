#!/bin/bash
set -euo pipefail

# Oracle solution: write the reference answer built from the TavernBench label.
mkdir -p /app
cat > /app/answer.json <<'TAVERNBENCH_ANSWER'
{oracle_answer}
TAVERNBENCH_ANSWER
