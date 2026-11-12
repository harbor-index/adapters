#!/bin/bash
set -euo pipefail
/usr/bin/mkdir -p /logs/verifier
/usr/bin/chmod 700 /logs/verifier
/usr/bin/rm -f /logs/verifier/reward.txt /logs/verifier/reward.json /logs/verifier/result.json
printf '0\n' > /logs/verifier/reward.txt
exec /usr/local/bin/python -I /tests/verify.py
