#!/bin/bash
# Oracle: submit the gold test labels (reference upper bound -> reward 1.0).
set -euo pipefail
python /solution/gold.py /workspace/submission.csv
