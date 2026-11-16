#!/bin/bash
set -euo pipefail
# Trusted verifier.py writes /logs/verifier/reward.json on scored or invalid submissions.
# Run as root in Harbor's separate no-network verifier. Submitted Python is
# executed only after privilege drop; native scoring remains in this process.
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
cd /tests
python -I /tests/verifier.py
