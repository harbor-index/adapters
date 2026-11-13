#!/bin/sh
# verify.py runs upstream grading and writes /logs/verifier/reward.txt.
set -eu
exec /usr/local/bin/python /tests/verify.py
