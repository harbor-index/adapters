#!/bin/bash
# Original-side parity run: unmodified upstream TaxCalcBench at the pinned
# commit, one run over the parity cases.
#
# Upstream's CLI writes outputs into its own results/ tree (where the
# leaderboard's saved outputs live), so use a separate, throwaway checkout of
# the pinned commit. Each run's files go to $OUT_DIR/run_<N>/<task_id>/, with
# upstream's full log for that case in run.log. A run directory is never
# reused: pick a new RUN number for every attempt.
#
#   export OPENAI_API_KEY=...            # parity key
#   export OPENAI_BASE_URL=https://app.ppapi.ai/v1  OPENAI_API_BASE=$OPENAI_BASE_URL
#   TAXCALCBENCH_ROOT=/path/to/clean/checkout RUN=1 OUT_DIR=parity_runs/original \
#     [CASES_FILE=parity/parity_cases.txt] [LIMIT=5] ./parity/run_original.sh
set -euo pipefail
PINNED_SHA=fdfe0db992ae9033f5b8b942633181a3596da64c
: "${TAXCALCBENCH_ROOT:?set TAXCALCBENCH_ROOT to a clean checkout of the pinned commit}"
: "${OPENAI_API_KEY:?}"
: "${RUN:?set RUN to the run number (1, 2, 3)}"
: "${OUT_DIR:?}"
MODEL=${MODEL:-gpt-6.2-sol}
LEVEL=${THINKING_LEVEL:-high}
CASES_FILE=${CASES_FILE:-$(dirname "$0")/parity_cases.txt}
CASES_FILE=$(cd "$(dirname "$CASES_FILE")" && pwd)/$(basename "$CASES_FILE")
OUT_DIR=$(mkdir -p "$OUT_DIR" && cd "$OUT_DIR" && pwd)
LIMIT=${LIMIT:-0}
RUN_DIR="$OUT_DIR/run_${RUN}"

if [ -e "$RUN_DIR" ]; then
  echo "refusing to reuse $RUN_DIR; choose a new RUN number" >&2
  exit 1
fi
head_sha=$(git -C "$TAXCALCBENCH_ROOT" rev-parse HEAD)
if [ "$head_sha" != "$PINNED_SHA" ]; then
  echo "upstream checkout is at $head_sha, expected $PINNED_SHA" >&2
  exit 1
fi
mkdir -p "$RUN_DIR"
{
  echo "upstream commit: $head_sha"
  echo "model: openai/$MODEL  thinking level: $LEVEL"
  echo "started: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$RUN_DIR/run.meta"

cd "$TAXCALCBENCH_ROOT"
n=0
grep -v '^#' "$CASES_FILE" | while read -r task_id year case_name; do
  [ -z "$task_id" ] && continue
  n=$((n + 1))
  if [ "$LIMIT" -gt 0 ] && [ "$n" -gt "$LIMIT" ]; then break; fi
  results="tax_calc_bench/$year/results/$case_name/openai/$MODEL"
  dest="$RUN_DIR/$task_id"
  mkdir -p "$dest"
  # Start from an empty upstream output slot so nothing saved earlier is reused.
  rm -f "$results"/*"_${LEVEL}_1.md"
  uv run python -m tax_calc_bench.main --provider openai --model "$MODEL" \
    --thinking-level "$LEVEL" --tax-year "$year" --test-name "$case_name" \
    --save-outputs --num-runs 1 < /dev/null > "$dest/run.log" 2>&1 \
    || echo "upstream exited non-zero: $task_id" | tee -a "$RUN_DIR/run.meta"
  if ls "$results"/*"_${LEVEL}_1.md" > /dev/null 2>&1; then
    mv "$results"/*"_${LEVEL}_1.md" "$dest/"
  else
    echo "no output saved: $task_id" | tee -a "$RUN_DIR/run.meta"
  fi
done
echo "finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$RUN_DIR/run.meta"
