#!/bin/bash
# Mirrors LoopsBench's OracleAgent (loopsbench/agents/oracle_agent.py): task-level
# files go to /task, gold patch shards go to /workspace/gold_patches, then the
# upstream solution.sh runs unchanged.
set -e
mkdir -p /task /workspace
cp -a /solution/task_files/. /task/
if [ -d /solution/gold_patches ]; then
  rm -rf /workspace/gold_patches
  cp -a /solution/gold_patches /workspace/gold_patches
fi
cd /workspace
bash /task/solution.sh
