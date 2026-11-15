#!/bin/bash
# Placeholder. CWE-bench tasks ship their own deterministic verifier (tests/test.sh,
# which runs tests/verify_programmatic.py). The adapter copies it verbatim and never
# writes this file into a generated task. If this ever runs, fail closed.
echo "cwe-bench adapter: placeholder verifier ran; the upstream tests/ are missing." >&2
mkdir -p /logs/verifier
echo 0 > /logs/verifier/reward.txt
exit 1
