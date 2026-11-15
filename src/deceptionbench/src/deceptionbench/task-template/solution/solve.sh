#!/bin/bash
set -euo pipefail
mkdir -p /workspace
python -I - <<'PY'
import base64
from pathlib import Path
Path("/workspace/response.txt").write_bytes(base64.b64decode("{oracle_base64}"))
PY
