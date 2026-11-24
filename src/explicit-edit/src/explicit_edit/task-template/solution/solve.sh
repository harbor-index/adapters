#!/bin/bash
# Oracle: overwrite the workspace with the upstream expected files, byte for byte.
# Input and expected always contain the same file set, so no deletions are needed.
set -euo pipefail
cp -a /solution/expected/. /workspace/
