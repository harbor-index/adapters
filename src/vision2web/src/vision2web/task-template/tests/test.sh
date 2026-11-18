#!/bin/bash
# verify.py writes /logs/verifier/reward.txt after real model grading only.
set -euo pipefail
umask 077
exec python /tests/verify.py
