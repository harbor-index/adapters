#!/bin/bash
set -euo pipefail
cp -- "$(dirname -- "${BASH_SOURCE[0]}")/reference.txt" /workspace/response.txt
