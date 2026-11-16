#!/bin/bash
# Verifier: original evaluate_prepare.py + evaluate.py, then AIRS-Bench normalized score.
# The reward is written to /logs/verifier/reward.txt (LOGS_DIR overrides the dir for local runs).
set -uo pipefail
HERE="${TESTS_DIR:-/tests}"
WORKSPACE="${WORKSPACE:-/workspace}"
LOGS="${LOGS_DIR:-/logs/verifier}"
mkdir -p "$LOGS"
WORK=$(mktemp -d)
mkdir -p "$WORK/raw" "$WORK/data"

fail() { echo "$1"; echo 0 > "$LOGS/reward.txt"; exit 0; }

[ -f "$WORKSPACE/submission.csv" ] || fail "No $WORKSPACE/submission.csv found -> reward 0"

python "$HERE/download_data.py" --out "$WORK/raw" || fail "could not download test data"
(cd "$HERE" && python evaluate_prepare.py \
    --global-shared-data-dir "$WORK/raw" \
    --agent-data-mount-dir "$WORK/data" \
    --agent-log-dir "$WORKSPACE") || fail "evaluate_prepare.py failed"

cp "$HERE"/evaluate.py "$WORK"/
for f in utils.py custom_labels.py testing_util.py pyext.py; do [ -f "$HERE/$f" ] && cp "$HERE/$f" "$WORK"/; done
cd "$WORK"
python evaluate.py --submission-file data/submission.csv > "$WORK/eval_stdout.txt" 2> "$WORK/eval_stderr.txt"
cat "$WORK/eval_stdout.txt"; tail -n 20 "$WORK/eval_stderr.txt"

python "$HERE/score.py" "$WORK/eval_stdout.txt" "$HERE/score_config.json" "$LOGS/reward.txt"
exit 0
