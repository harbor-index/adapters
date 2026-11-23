#!/bin/bash
set -euo pipefail

cat << 'EOF' > /app/answer.json
{oracle_answer_json}
EOF
