#!/bin/bash
# Oracle: deliver the task's golden workbook as the solution.
set -euo pipefail

cp /solution/golden.xlsx /workspace/solution.xlsx
echo "Oracle: copied the golden workbook to /workspace/solution.xlsx"
