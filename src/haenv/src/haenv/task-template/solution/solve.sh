#!/bin/bash
set -euo pipefail

# Oracle solution generated per HAEnv M2 case:
# 1. Orders required key signals via haenv-ehr within budget.
# 2. Writes the gold diagnosis + management plan JSON to /workspace/answer.json.

{order_commands}

cat > /workspace/answer.json <<'HAENV_ORACLE_EOF'
{oracle_answer_json}
HAENV_ORACLE_EOF
