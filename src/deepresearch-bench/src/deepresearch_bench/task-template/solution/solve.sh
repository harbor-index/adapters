#!/bin/bash
set -e

echo "Executing oracle solution..."
if [ ! -f /solution/report.pdf ] || [ ! -f /solution/oracle_token.txt ]; then
    echo "ERROR: No reference report for this task in /solution/"
    exit 1
fi
# Keep the reference report for inspection; it is not what gets graded.
cp /solution/report.pdf /workspace/reference_report.pdf
# The secret token proves the oracle ran; test.sh checks its hash.
cp /solution/oracle_token.txt /workspace/.oracle_token
echo "Solution completed!"
