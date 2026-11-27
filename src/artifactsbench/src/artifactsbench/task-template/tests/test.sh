#!/bin/sh
set -eu
# verify.py writes the 0-1 reward here only after the judge returns a valid score.
exec python -I /tests/verify.py /logs/verifier/reward.txt
