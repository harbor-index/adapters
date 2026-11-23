#!/bin/bash
set -euo pipefail

# Reference output for this page. It is synthesised from the page's unit tests
# (the original benchmark ships no reference transcriptions), so it satisfies
# every test but is not a faithful transcription of the document.
cp /solution/oracle.md /workspace/output.md
echo "Wrote /workspace/output.md ($(wc -c < /workspace/output.md) bytes)"
