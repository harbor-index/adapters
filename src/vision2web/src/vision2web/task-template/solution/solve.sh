#!/bin/bash
set -euo pipefail
if [ ! -f /solution/reference/start.sh ]; then
    echo 'No reference candidate supplied. Generate with --reference-dir after review.' >&2
    exit 64
fi
cp -a /solution/reference/. /workspace/
# The real verifier evaluates this website; there is no oracle score shortcut.
