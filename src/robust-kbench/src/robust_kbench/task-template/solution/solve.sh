#!/bin/bash
set -euo pipefail

echo "Deploying Oracle reference CUDA kernel..."
if [ -f /solution/reference.cu ]; then
    cp /solution/reference.cu /app/kernel.cu
    echo "Oracle kernel deployed to /app/kernel.cu"
else
    echo "ERROR: /solution/reference.cu not found!"
    exit 1
fi
