#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH="/opt/shade${PYTHONPATH:+:$PYTHONPATH}"
python /solution/oracle.py
