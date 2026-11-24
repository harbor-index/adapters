#!/bin/bash
set -euo pipefail

mkdir -p /workspace
cat > /workspace/predictions.json <<'MOTIFBENCH_ORACLE_EOF'
{oracle_predictions_json}
MOTIFBENCH_ORACLE_EOF
echo "Oracle predictions written to /workspace/predictions.json"
