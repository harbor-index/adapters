#!/bin/bash
# Reference solver that uses only the files in /workspace.
#   Instruction-following tasks: follows the stated pipeline exactly (matches the reference output).
#   Modeling and forecasting tasks: a gradient-boosting baseline. Scores on these continuous
#   metrics are below 1.0 by nature; the held-out labels are never read.
set -euo pipefail
cd /workspace
python3 "/solution/{solver}" /solution/spec.json
test -f /workspace/prediction.csv
