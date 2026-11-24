#!/bin/bash
# Byte-exact comparison of /workspace against the upstream expected files.
# verify.py writes /logs/verifier/reward.json (reward + diagnostics) and reward.txt.
set -u
mkdir -p /logs/verifier
if ! python3 /tests/verify.py /workspace /tests/expected /logs/verifier; then
  echo "verify.py crashed; recording reward 0" >&2
  echo 0 > /logs/verifier/reward.txt
  rm -f /logs/verifier/reward.json
fi
exit 0
