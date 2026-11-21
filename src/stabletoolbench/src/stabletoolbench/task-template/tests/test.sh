#!/bin/sh
set -eu
python "$(dirname "$0")/grade.py"
test -s /logs/verifier/reward.txt
