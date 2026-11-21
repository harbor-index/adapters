#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
# The verifier writes /logs/verifier/reward.txt after successful scoring.
python3 verify.py
