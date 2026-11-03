#!/bin/bash
# Oracle: ship vLLM with default settings, which is the "vLLM default" row of the paper's
# Table 2. InferenceBench has no reference solution because the score is an unbounded
# speedup. This run checks the task and verifier end to end against published numbers; it
# is not an upper bound on the score.
set -euo pipefail

python3 -m pip install --no-warn-script-location \
    --extra-index-url https://download.pytorch.org/whl/cu128 \
    --constraint /solution/vllm-constraints.txt "vllm=={vllm_version}"
cp /solution/start_server.sh /home/agent/task/start_server.sh
chmod +x /home/agent/task/start_server.sh
