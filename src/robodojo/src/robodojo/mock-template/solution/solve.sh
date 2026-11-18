#!/usr/bin/env bash
set -euo pipefail
# Scripted player for the mock scene. It needs no model and no API key.
python3 "$(dirname "$0")/solve.py"
