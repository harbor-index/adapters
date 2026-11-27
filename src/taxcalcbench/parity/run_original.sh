#!/bin/bash
# Original-side parity run: unmodified upstream TaxCalcBench at the pinned
# commit, one run over the parity cases, one case (one model call) at a time.
#
# Where upstream runs. The script exports the pinned commit from the checkout's
# git objects into a private temporary folder (code, prompts and test data;
# upstream's saved leaderboard outputs are left out) and runs upstream's CLI
# there. uv takes the environment from the checkout (`<checkout>/.venv`, which
# git ignores, built from the checkout's uv.lock with --locked); no tracked file
# of the checkout is written: the script stops if start-up changes one and
# prints a warning if one changes during the cases. The export is
# new, used by this launcher alone, and holds no earlier output, so a result
# this script records was written by a call it started. The export is removed
# when the script ends (a SIGKILL of the script leaves it behind).
#
# What is kept. Each run's files go to $OUT_DIR/run_<RUN>/<task_id>/, with
# upstream's full log for that case in run.log. Per case, status.tsv records:
# task_id, attempt number, upstream exit code, whether the model output and the
# evaluation file were saved, and a status (ok / generation_failed /
# evaluation_missing / in_progress). Upstream prints the cause of a failure to
# run.log; it has no machine-readable error type. The script exits 2 when any
# attempted case is not "ok".
#
# Running a case again. A run folder is never silently reused. A case that
# failed or was cut off (API error, timeout, interruption) can be attempted
# again with RERUN_FAILED=1 and the same RUN: the settings must equal the first
# attempt's (run.settings), cases already "ok" are left alone, and the files of
# earlier attempts are kept under attempt_<k>/. An attempt that was cut off is
# always run again; whatever it had saved is kept with that attempt and never
# counted.
#
# Stopping. Ctrl-C, Ctrl-\, `kill` or a closed terminal during a case stops upstream's
# processes at once and writes status.tsv; during start-up (uv builds the
# environment on first use) it takes effect when that step ends and nothing has
# been recorded yet. One launcher works on a run folder at a time.
#
# From the repository root:
#   export OPENAI_API_KEY=...
#   export OPENAI_BASE_URL=...          # same endpoint as the Harbor side
#   TAXCALCBENCH_ROOT=/path/to/tax-calc-bench RUN=1 OUT_DIR=parity-experiments/full-original \
#     [CASES_FILE=src/taxcalcbench/parity/sanity_cases.txt] [LIMIT=5] [RERUN_FAILED=1] \
#     bash src/taxcalcbench/parity/run_original.sh
set -euo pipefail
# The pinned upstream commit. TAXCALCBENCH_EXPECTED_REF may name another full
# commit hash for one case only: a model upstream's TY2025 runner does not
# list, which needs a fork whose only change widens that list. The commit used
# is written to run.settings and must be the same on the Harbor side.
PINNED_SHA=${TAXCALCBENCH_EXPECTED_REF:-fdfe0db992ae9033f5b8b942633181a3596da64c}
: "${TAXCALCBENCH_ROOT:?set TAXCALCBENCH_ROOT to a clean checkout of the pinned commit}"
: "${OPENAI_API_KEY:?}"
: "${RUN:?set RUN to the run label (1, 2, 3, sanity)}"
: "${OUT_DIR:?}"
MODEL=${MODEL:-gpt-6-luna}
LEVEL=${THINKING_LEVEL:-high}
CASES_FILE=${CASES_FILE:-$(dirname "$0")/full_cases.txt}
LIMIT=${LIMIT:-0}
RERUN_FAILED=${RERUN_FAILED:-0}
API_BASE=${OPENAI_BASE_URL:-${OPENAI_API_BASE:-https://api.openai.com/v1}}

# Messages go through an external command. When their reader is gone (closed
# terminal or pipe) the write fails there, and nothing is left in this shell's
# own output buffer to end up in a file written later.
trap '' PIPE
say() { /bin/echo "$@" || true; }
warn() { /bin/echo "$@" >&2 || true; }
die() { warn "$*"; exit 1; }

case "$LIMIT" in ''|*[!0-9]*) die "LIMIT must be a non-negative integer, got '$LIMIT'" ;; esac
case "$RUN" in ''|*[!0-9A-Za-z_-]*) die "RUN must be a simple label such as 1, 2, 3" ;; esac
case "$MODEL" in ''|*[!0-9A-Za-z._-]*) die "MODEL must be a plain model name" ;; esac
case "$LEVEL" in ''|*[!0-9A-Za-z_-]*) die "THINKING_LEVEL must be a plain level name" ;; esac
[ -f "$CASES_FILE" ] || die "case list not found: $CASES_FILE"
CASES_FILE=$(cd "$(dirname "$CASES_FILE")" && pwd)/$(basename "$CASES_FILE")
[ -d "$TAXCALCBENCH_ROOT/tax_calc_bench" ] || die "not a TaxCalcBench checkout: $TAXCALCBENCH_ROOT"
TAXCALCBENCH_ROOT=$(cd "$TAXCALCBENCH_ROOT" && pwd)

# Case list: "task_id tax_year upstream_case" per line, plain names, no duplicates.
CASES=$(tr -d '\r' < "$CASES_FILE" | awk '!/^#/ && NF')
[ -n "$CASES" ] || die "no cases in $CASES_FILE"
# awk reads its whole input and prints one line, so no pipe is closed on a writer (long lists included).
bad=$(printf '%s\n' "$CASES" | awk '(NF != 3 || $2 !~ /^ty2[45]$/ || $1 !~ /^[A-Za-z0-9][A-Za-z0-9._-]*$/ || $3 !~ /^[A-Za-z0-9][A-Za-z0-9._-]*$/) && !n++ {b = $0} END {if (n) print b}')
[ -z "$bad" ] || die "bad line in $CASES_FILE: '$bad' (expected: task_id ty24|ty25 upstream_case)"
# Compared without regard to letter case: on a case-insensitive disk two such ids share one folder.
dup=$(printf '%s\n' "$CASES" | awk '{print $1}' | sort -f | uniq -di | awk 'NR == 1 {d = $0} END {if (NR) print d}')
[ -z "$dup" ] || die "task id listed twice in $CASES_FILE: $dup"

# The checkout supplies the commit and the uv environment (pyproject.toml and
# uv.lock), so it must be the pinned commit with no local change. Its results/
# folders may differ: upstream's CLI saves outputs there when run by hand.
head_sha=$(git -C "$TAXCALCBENCH_ROOT" rev-parse HEAD)
[ "$head_sha" = "$PINNED_SHA" ] || die "upstream checkout is at $head_sha, expected $PINNED_SHA"
clean_check() {
  # Explicit options, so the user's git config cannot hide untracked files.
  git -C "$TAXCALCBENCH_ROOT" -c core.fileMode=true status --porcelain --untracked-files=all -- . \
    ':(exclude)tax_calc_bench/ty24/results' ':(exclude)tax_calc_bench/ty25/results'
}
dirty=$(clean_check)
if [ -n "$dirty" ]; then
  die "upstream checkout has local changes outside results/:
$(printf '%s\n' "$dirty" | sed -n '1,5p')"
fi

# The endpoint as it is recorded and compared with the Harbor side: scheme, host, port and path, no credentials.
API_RECORD=$(python3 -c 'import sys
from urllib.parse import urlsplit
u = urlsplit(sys.argv[1])
print(f"{u.scheme}://{u.hostname}{chr(58) + str(u.port) if u.port else str()}{u.path.rstrip(chr(47))}" if u.scheme and u.hostname else "")' "$API_BASE")
[ -n "$API_RECORD" ] || die "cannot read a scheme and host name from the API base URL"
# Upstream's client reads either variable; give it one endpoint under both names.
export OPENAI_BASE_URL="$API_BASE" OPENAI_API_BASE="$API_BASE"

mkdir -p "$OUT_DIR"
OUT_DIR=$(cd "$OUT_DIR" && pwd)
RUN_DIR="$OUT_DIR/run_${RUN}"

# One launcher per run folder. The lock is an flock on an open file: it ends
# when this process ends, however it ends, so there is no stale state. The
# export, the start-up step, the checkout checks made while the lock is held
# and upstream's calls are started without the descriptor; another short helper
# command that was running when the script was killed can hold the lock for the
# moment it takes to finish.
exec 9> "$OUT_DIR/.run_${RUN}.lock"
python3 -c 'import fcntl, sys
try:
    fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.exit(1)' || die "another run_original.sh is working on $RUN_DIR"

WORK=""
child=""
ledger_ready=0
cur_dest=""; cur_out=""; cur_eval=""
# status.tsv: one row per attempted case, in case-list order, from the per-case status files.
ledger() {
  [ "$ledger_ready" = 1 ] || return 0
  printf 'task_id\tattempt\tupstream_exit\tmodel_output\tevaluation\tstatus\n' > "$RUN_DIR/status.tsv"
  while read -r task_id _; do
    if [ -f "$RUN_DIR/$task_id/status" ]; then cat "$RUN_DIR/$task_id/status" >> "$RUN_DIR/status.tsv"
    # A case folder without a status: a rerun was stopped while it filed the earlier attempt.
    elif [ -d "$RUN_DIR/$task_id" ]; then printf '%s\t\t\t\t\tin_progress\n' "$task_id" >> "$RUN_DIR/status.tsv"; fi
  done < <(printf '%s\n' "$CASES")
}
stop_child() {
  # A signal can arrive after upstream was started and before its pid was noted.
  [ -n "$child" ] || child=$(jobs -p | tail -1)
  [ -n "$child" ] || return 0
  # Upstream runs in its own process group, so the whole call stops.
  kill -TERM -- "-$child" 2>/dev/null || kill -TERM "$child" 2>/dev/null || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    kill -0 "$child" 2>/dev/null || break
    sleep 1
  done
  kill -KILL -- "-$child" 2>/dev/null || true
  wait "$child" 2>/dev/null || true
  child=""
}
# Files a cut-off attempt had already saved stay with that attempt (its status
# is not "ok", so they are never counted; a rerun files them under attempt_<k>/).
keep_partial() {
  [ -n "$cur_dest" ] && [ -d "$cur_dest" ] || return 0
  for f in "$cur_out" "$cur_eval"; do
    if [ -n "$f" ] && [ -f "$f" ]; then mv "$f" "$cur_dest/" 2>/dev/null || true; fi
  done
}
interrupted() {
  trap '' INT TERM HUP QUIT PIPE
  stop_child
  keep_partial
  ledger
  if [ "$ledger_ready" = 1 ]; then
    warn "interrupted; stopped (the case in progress can be run again with RERUN_FAILED=1)"
  else
    warn "interrupted during start-up; nothing was recorded"
  fi
  exit 130
}
# However the script ends, no call of upstream is left running and the export goes.
cleanup() { stop_child; if [ -n "$WORK" ]; then rm -rf "$WORK" "$WORK.index"; fi; }
trap cleanup EXIT
trap interrupted INT TERM HUP QUIT

# Private export of the commit: tracked files only, straight from the git
# objects, without upstream's saved outputs.
WORK=$(mktemp -d "${TMPDIR:-/tmp}/taxcalcbench-upstream.XXXXXX" 9>&-)
WORK=$(cd "$WORK" && pwd -P)
(
  exec 9>&-  # in a subshell that has closed the lock descriptor, so no git process gets a copy
  GIT_INDEX_FILE="$WORK.index" git -C "$TAXCALCBENCH_ROOT" read-tree "$head_sha" || exit 1
  GIT_INDEX_FILE="$WORK.index" git -C "$TAXCALCBENCH_ROOT" ls-files -z \
    | { grep -zv '^tax_calc_bench/ty2[45]/results/' || [ $? -eq 1 ]; } \
    | GIT_INDEX_FILE="$WORK.index" git -C "$TAXCALCBENCH_ROOT" -c core.autocrlf=false checkout-index -z --stdin --prefix="$WORK/"
) || die "could not export $head_sha from $TAXCALCBENCH_ROOT (see the message above)"
rm -f "$WORK.index"
[ -f "$WORK/tax_calc_bench/main.py" ] || die "the export of $head_sha has no tax_calc_bench/main.py"

# Upstream's CLI loads a .env file from its folder or any parent folder, which
# could change settings for this side only.
dotenv_seen=""
d="$WORK/tax_calc_bench"
while :; do
  if [ -f "$d/.env" ]; then
    [ "${ALLOW_DOTENV:-0}" = 1 ] || die "found $d/.env, which upstream would load; remove it or set ALLOW_DOTENV=1"
    dotenv_seen=$d/.env
  fi
  if [ "$d" = / ]; then break; fi
  d=$(dirname "$d")
done

cd "$WORK"
# uv takes the environment from the checkout (--project) and runs in this
# folder, so Python loads upstream's code and data from the export. --locked
# keeps the checkout's uv.lock as it is: uv stops if the lock would need a change.
# Upstream files a result under its canonical model and level names. It reads
# its test data in the shell's text encoding, and the Harbor tasks hold the
# UTF-8 reading, so anything else would send a different prompt on this side.
facts=$(exec 9>&-; uv run --locked --project "$TAXCALCBENCH_ROOT" python -c 'import codecs, importlib.metadata as md, locale, os, platform, sys
import tax_calc_bench
from tax_calc_bench.config import canonicalize_model_name, canonicalize_thinking_level, openai_reasoning_effort
encoding = locale.getpreferredencoding(False)
if codecs.lookup(encoding).name != "utf-8":
    sys.exit(f"upstream would read its test data as {encoding}; use a UTF-8 locale or set PYTHONUTF8=1")
model, level = canonicalize_model_name("openai", sys.argv[1]), canonicalize_thinking_level(sys.argv[2])
print("NAMES", model, level, openai_reasoning_effort(model, level))
print("VERSIONS", md.version("litellm"), md.version("openai"), platform.python_version())
print("MODULE", os.path.realpath(os.path.dirname(tax_calc_bench.__file__)))' \
  "$MODEL" "$LEVEL" < /dev/null) || die "could not start upstream's Python environment (see the message above)"
field() { printf '%s\n' "$facts" | awk -v key="$1" -v n="$2" '$1 == key {v = $n} END {print v}'; }
module_dir=$(printf '%s\n' "$facts" | sed -n 's/^MODULE //p' | tail -1)
[ "$module_dir" = "$WORK/tax_calc_bench" ] || die "Python loaded tax_calc_bench from '$module_dir', not from the export $WORK"
SLOT_MODEL=$(field NAMES 2); SLOT_LEVEL=$(field NAMES 3); EFFORT=$(field NAMES 4)
LITELLM_VERSION=$(field VERSIONS 2); OPENAI_VERSION=$(field VERSIONS 3); PYTHON_VERSION=$(field VERSIONS 4)
case "$SLOT_MODEL" in ''|*[!0-9A-Za-z._-]*) die "could not get upstream's canonical name for model '$MODEL'" ;; esac
case "$SLOT_LEVEL" in ''|*[!0-9A-Za-z_-]*) die "could not get upstream's canonical name for thinking level '$LEVEL'" ;; esac
case "$EFFORT" in ''|None|*[!0-9A-Za-z_-]*) die "upstream has no reasoning effort for model '$SLOT_MODEL' at thinking level '$SLOT_LEVEL'" ;; esac
case "$LITELLM_VERSION" in ''|*[!0-9A-Za-z.+_-]*) die "could not read the LiteLLM version of upstream's environment" ;; esac
case "$OPENAI_VERSION" in ''|*[!0-9A-Za-z.+_-]*) die "could not read the OpenAI SDK version of upstream's environment" ;; esac
[ -z "$(exec 9>&-; clean_check)" ] || die "starting upstream's environment changed a tracked file of the checkout; restore it first"

settings=$(printf 'upstream_commit=%s\nmodel=openai/%s\nthinking_level=%s\nreasoning_effort=%s\napi_base=%s\nlitellm=%s\nopenai=%s\ncases_sha256=%s\nlimit=%s\n' \
  "$head_sha" "$SLOT_MODEL" "$SLOT_LEVEL" "$EFFORT" "$API_RECORD" "$LITELLM_VERSION" "$OPENAI_VERSION" \
  "$(shasum -a 256 "$CASES_FILE" | cut -d' ' -f1)" "$LIMIT")

# A run folder that a stopped launcher made and never wrote into is no run.
if [ -d "$RUN_DIR" ]; then rmdir "$RUN_DIR" 2>/dev/null || true; fi
if [ -e "$RUN_DIR" ] && [ "$RERUN_FAILED" != 1 ]; then
  die "refusing to reuse $RUN_DIR; choose a new RUN label, or set RERUN_FAILED=1 to run its failed cases again"
fi
if [ "$RERUN_FAILED" = 1 ]; then
  [ -f "$RUN_DIR/run.settings" ] || die "RERUN_FAILED=1 but $RUN_DIR has no run.settings"
  if [ "$settings" != "$(cat "$RUN_DIR/run.settings")" ]; then
    die "settings differ from the first attempt of this run ($RUN_DIR/run.settings):
$(diff <(printf '%s\n' "$settings") "$RUN_DIR/run.settings" || true)"
  fi
else
  mkdir -p "$RUN_DIR"
  printf '%s\n' "$settings" > "$RUN_DIR/run.settings"
  # Written once: a copy of the folder keeps it and a rerun does not change it.
  printf '%s-%s-%s\n' "$(date -u +%Y%m%dT%H%M%SZ)" "$$" "$RANDOM$RANDOM" > "$RUN_DIR/run.id"
fi
ledger_ready=1
{
  if [ "$RERUN_FAILED" = 1 ]; then echo "--- rerun of failed cases ---"; fi
  echo "started: $(date -u +%Y-%m-%dT%H:%M:%SZ) (python $PYTHON_VERSION, launcher pid $$)"
  if [ -n "$dotenv_seen" ]; then echo "dotenv_allowed: $dotenv_seen"; fi
} >> "$RUN_DIR/run.meta"

n=0
while read -r task_id year case_name; do
  n=$((n + 1))
  if [ "$LIMIT" -gt 0 ] && [ "$n" -gt "$LIMIT" ]; then break; fi
  results="tax_calc_bench/$year/results/$case_name/openai/$SLOT_MODEL"
  dest="$RUN_DIR/$task_id"
  out_file="$results/model_completed_return_${SLOT_LEVEL}_1.md"
  eval_file="$results/evaluation_result_${SLOT_LEVEL}_1.md"
  attempt=1
  if [ -d "$dest" ]; then
    prev_status=""; prev_attempt=0
    if [ -f "$dest/status" ]; then
      prev_status=$(cut -f6 "$dest/status"); prev_attempt=$(cut -f2 "$dest/status")
    fi
    if [ "$prev_status" = ok ]; then continue; fi
    archived=$(find "$dest" -maxdepth 1 -type d -name 'attempt_*' | wc -l | tr -d ' ')
    case "$prev_attempt" in ''|*[!0-9]*) prev_attempt=0 ;; esac
    pending=$(find "$dest" -maxdepth 1 -type f | wc -l | tr -d ' ')
    if [ "$pending" -gt 0 ]; then
      # Keep whatever the earlier attempt left, finished or cut off.
      # The files belong to the attempt their status names. A stop while they
      # were being filed leaves some of them in attempt_<k>/ already: the rest
      # goes to the same folder. Without a usable status, or when that folder
      # already holds a status, they get the next free number.
      if [ "$prev_attempt" -lt 1 ] || { [ "$prev_attempt" -le "$archived" ] && [ -e "$dest/attempt_$prev_attempt/status" ]; }; then
        prev_attempt=$((archived + 1))
      fi
      attempt=$((prev_attempt + 1))
      mkdir -p "$dest/attempt_$prev_attempt"
      for f in "$dest"/run.log "$dest"/*.md "$dest"/status; do
        if [ -f "$f" ]; then mv "$f" "$dest/attempt_$prev_attempt/"; fi
      done
    else
      # Nothing to file: the folder was made (or its files were filed) just before a stop.
      attempt=$((archived + 1))
    fi
  fi
  mkdir -p "$dest"
  rm -f "$out_file" "$eval_file"
  printf '%s\t%s\t\t\t\tin_progress\n' "$task_id" "$attempt" > "$dest/status"
  cur_dest=$dest; cur_out=$out_file; cur_eval=$eval_file
  # Upstream's CLI, in its own session so that stopping this script stops it too.
  python3 -c 'import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' \
    uv run --locked --project "$TAXCALCBENCH_ROOT" python -m tax_calc_bench.main --provider openai --model "$SLOT_MODEL" \
    --thinking-level "$SLOT_LEVEL" --tax-year "$year" --test-name "$case_name" \
    --save-outputs --num-runs 1 < /dev/null > "$dest/run.log" 2>&1 9>&- &
  child=$!
  rc=0
  wait "$child" || rc=$?
  # Nothing in this call's process group may outlive it and write into a later case's files.
  kill -KILL -- "-$child" 2>/dev/null || true
  child=""
  has_out=no; has_eval=no
  if [ -s "$out_file" ]; then mv "$out_file" "$dest/"; has_out=yes; fi
  if [ -f "$eval_file" ] && grep -q '^Strictly correct return: ' "$eval_file"; then mv "$eval_file" "$dest/"; has_eval=yes; fi
  if [ "$has_eval" = yes ] && [ "$has_out" = yes ]; then status=ok
  elif [ "$has_out" = yes ]; then status=evaluation_missing
  else status=generation_failed; fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$task_id" "$attempt" "$rc" "$has_out" "$has_eval" "$status" > "$dest/status"
  cur_dest=""; cur_out=""; cur_eval=""
  say "$task_id: $status (attempt $attempt)"
done < <(printf '%s\n' "$CASES")

ledger
[ -z "$(exec 9>&-; clean_check)" ] || warn "warning: the checkout changed during the run (outside results/)"
attempted=$(($(wc -l < "$RUN_DIR/status.tsv") - 1))
ok=$(awk -F'\t' 'NR > 1 && $6 == "ok"' "$RUN_DIR/status.tsv" | wc -l | tr -d ' ')
{
  echo "finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "complete: $ok/$attempted cases ok"
} >> "$RUN_DIR/run.meta"
say "complete: $ok/$attempted cases ok (details: $RUN_DIR/status.tsv)"
if [ "$ok" -ne "$attempted" ] || [ "$attempted" -eq 0 ]; then
  exit 2
fi
