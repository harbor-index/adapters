#!/bin/bash
# Scores /workspace/output.md against this page's unit tests.
# Writes the reward (fraction of tests passed) to /logs/verifier/reward.txt and
# per-test results to /logs/verifier/olmocr_bench_results.json.
set -uo pipefail

LOG_DIR=/logs/verifier
mkdir -p "$LOG_DIR"

# The reward always comes from this run: drop anything written earlier and start
# from 0, so that every path through this script leaves a reward it wrote itself.
rm -f "$LOG_DIR/reward.json" "$LOG_DIR/olmocr_bench_results.json"
echo 0 > "$LOG_DIR/reward.txt"

# The equation renderer caches rendered formulas under ~/.cache. Use a fresh,
# empty home directory so that no cache from before scoring is reused.
HOME="$(mktemp -d)"
export HOME
export PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

/usr/local/bin/python3 -I /tests/evaluate.py \
  --markdown /workspace/output.md \
  --output-dir "$LOG_DIR"
status=$?

if [ "$status" -ne 0 ]; then
  # The scoring harness itself failed (not the submission). The reward stays 0
  # and no per-test results are kept, which marks the run as unscored.
  echo "Scoring harness failed with exit code $status; reward set to 0." >&2
  rm -f "$LOG_DIR/olmocr_bench_results.json"
  echo 0 > "$LOG_DIR/reward.txt"
fi
exit "$status"
