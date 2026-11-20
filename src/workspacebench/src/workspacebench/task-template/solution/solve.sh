#!/bin/bash
# Copy the reference outputs into the folder the instruction names.
set -euo pipefail

if [ -z "$(ls -A /solution/output 2>/dev/null)" ]; then
    echo "No reference outputs for this task" >&2
    exit 1
fi
mkdir -p /app/model_output
cp -r /solution/output/. /app/model_output/
