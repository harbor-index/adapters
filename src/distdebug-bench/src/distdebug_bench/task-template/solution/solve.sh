#!/bin/bash
# Oracle: write the human-labeled root-cause blocks as the diagnosis.
set -euo pipefail
cd /workspace
cat > diagnosis.json <<'DISTDEBUG_ORACLE_EOF'
{oracle_diagnosis}
DISTDEBUG_ORACLE_EOF
python3 .validate_diagnosis.py diagnosis.json
