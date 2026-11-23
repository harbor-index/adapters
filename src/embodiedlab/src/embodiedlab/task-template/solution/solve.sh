#!/bin/bash
# Oracle: replay a recorded successful T0 run of this instance through the public tool API (see replay.py).
set -euo pipefail
cd /home/agent/workspace
PYTHONPATH=/home/agent/workspace python3 /solution/replay.py /solution/calls.jsonl.xz
