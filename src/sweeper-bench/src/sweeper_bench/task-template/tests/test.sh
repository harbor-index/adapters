#!/usr/bin/env bash
# SWEeper-Bench verifier for Harbor. Runs in the separate verifier environment
# (fresh upstream evaluation product image) after the agent's container is gone.
#
# Follows evaluate_case() in the upstream runtime/worker.py for one phase:
#   reset the buggy checkout -> apply the agent's patch -> start a fresh product
#   -> run the browser verifier (runtime/browser.py, unchanged) on the target and
#   preservation workflows -> retry the phase up to 3 times while the verifier
#   agent reports `uncertain`.
# Reward is 1 only when both the target and the preservation workflows pass.
#
# Verifier settings come from [verifier.env] (task.toml):
#   SWEEPER_VERIFIER_HARNESS   codex (default) | browser-use
#   SWEEPER_VERIFIER_MODEL     default gpt-5.6-luna (upstream default)
#   SWEEPER_VERIFIER_EFFORT    default xhigh (upstream default)
#   SWEEPER_VERIFIER_AUTH      api (OPENAI_API_KEY) | subscription (CODEX_AUTH_JSON)
#   SWEEPER_EVALUATE_EMPTY_PATCH  1 = verify an empty diff as the upstream
#                              `baseline` phase; 0 (default) = reward 0 without
#                              a verifier call, as upstream skips empty patches.
set -uo pipefail

LOGS=/logs/verifier
REWARD_FILE=/logs/verifier/reward.txt
EX=/sweeper-exchange
OUT="$EX/output"
PRODUCT=/workspace/product
PATCH_SRC=/logs/artifacts/prediction.patch
WORKFLOWS=/tests/workflows.yaml
CATEGORIES=target,preservation
BASE_COMMIT="$(git -C "$PRODUCT" rev-parse HEAD)"

HARNESS="${SWEEPER_VERIFIER_HARNESS:-codex}"
MODEL="${SWEEPER_VERIFIER_MODEL:-gpt-5.6-luna}"
EFFORT="${SWEEPER_VERIFIER_EFFORT:-xhigh}"
AUTH="${SWEEPER_VERIFIER_AUTH:-api}"
PROVIDER="${SWEEPER_VERIFIER_PROVIDER:-openai}"
RECORD="${SWEEPER_VERIFIER_RECORD:-0}"
CODEX_TIMEOUT="${SWEEPER_CODEX_TIMEOUT_SECONDS:-1800}"

mkdir -p "$LOGS"
log() { echo "[sweeper-verifier $(date -u +%H:%M:%S)] $*" | tee -a "$LOGS/test.log"; }

finish() {  # finish <reward> <status>
  echo "$1" >"$REWARD_FILE"
  log "status=$2 reward=$1"
  exit 0
}

# ---------------------------------------------------------------- rpc helper
rpc() {  # rpc <timeout_sec> <env_file|-> <argv...>
  local timeout="$1" envf="$2"
  shift 2
  local id
  id="$(date +%s%N)-$RANDOM"
  local dir="$EX/rpc/$id"
  mkdir -p "$dir"
  printf '%s\0' "$@" >"$dir/argv"
  if [ "$envf" != "-" ]; then cp "$envf" "$dir/env"; chmod 600 "$dir/env"; fi
  touch "$dir/ready"
  local waited=0
  while [ ! -f "$dir/done" ]; do
    sleep 2
    waited=$((waited + 2))
    if [ "$waited" -ge "$timeout" ]; then
      log "rpc timeout after ${timeout}s: $*"
      return 124
    fi
  done
  cat "$dir/stdout"
  cat "$dir/stderr" >&2
  rm -f "$dir/env"
  return "$(cat "$dir/exit")"
}

# ---------------------------------------------------------------- product
APP_PGID=""
PRE_PIDS=""
start_product() {
  # Same as a fresh upstream product container: run the image entrypoint setup.
  bash /opt/sweeper/image-setup.sh >>/tmp/product-dev-server.log 2>&1 || true
  PRE_PIDS="$(ps -eo pid= | tr -s ' \n' ' ')"
  setsid bash -lc "cd $PRODUCT && HOST=0.0.0.0 PORT=3000 BROWSER=none product-yarn start >/tmp/product-dev-server.log 2>&1" \
    </dev/null >/dev/null 2>&1 &
  APP_PGID=$!
}

stop_product() {
  # Upstream removes the whole product container. Stop the product's process
  # group, then any daemon that left it, sparing PID 1 and this script.
  if [ -n "$APP_PGID" ]; then
    kill -TERM -- "-$APP_PGID" 2>/dev/null || true
    sleep 3
    kill -KILL -- "-$APP_PGID" 2>/dev/null || true
  fi
  # Only processes created after start_product() are candidates; Harbor's own
  # exec processes (and the pipe reading this script's output) predate it.
  local pid ppid
  while read -r pid ppid; do
    case " $PRE_PIDS " in *" $pid "*) continue ;; esac
    [ "$pid" = "$$" ] || [ "$pid" = "$BASHPID" ] || [ "$ppid" = "$$" ] && continue
    kill -KILL "$pid" 2>/dev/null || true
  done < <(ps -eo pid=,ppid=)
  APP_PGID=""
  sleep 1
}

reset_workspace() {  # reset_workspace [patch]
  git -C "$PRODUCT" reset -q --hard "$BASE_COMMIT"
  git -C "$PRODUCT" clean -q -fdx
  [ -n "${1:-}" ] || return 0
  if git -C "$PRODUCT" apply "$1"; then return 0; fi
  log "git apply failed; retry excluding images"
  git -C "$PRODUCT" reset -q --hard "$BASE_COMMIT"
  git -C "$PRODUCT" clean -q -fdx
  git -C "$PRODUCT" apply \
    --exclude='*.png' --exclude='*.PNG' --exclude='*.jpg' --exclude='*.jpeg' \
    --exclude='*.JPG' --exclude='*.JPEG' --exclude='*.gif' --exclude='*.GIF' \
    --exclude='*.webp' --exclude='*.WEBP' --exclude='*.ico' --exclude='*.ICO' \
    --exclude='*.bmp' --exclude='*.BMP' "$1"
}

save_logs() {
  mkdir -p "$LOGS/sweeper"
  cp -a "$OUT/." "$LOGS/sweeper/" 2>/dev/null || true
  cp /tmp/product-dev-server.log "$LOGS/sweeper/product-dev-server.log" 2>/dev/null || true
  rm -rf "$LOGS/sweeper/secrets"
}

# ---------------------------------------------------------------- start
log "waiting for browser-verifier sidecar"
for _ in $(seq 1 300); do [ -f "$EX/server.ready" ] && break; sleep 1; done
if [ ! -f "$EX/server.ready" ]; then
  log "browser-verifier sidecar did not start"
  exit 1
fi

PATCH=""
if [ -s "$PATCH_SRC" ] && grep -qE '^diff --git ' "$PATCH_SRC"; then
  cp "$PATCH_SRC" "$EX/prediction.patch"
  cp "$PATCH_SRC" "$LOGS/prediction.patch"
  PATCH="$EX/prediction.patch"
  PHASE=prediction-scoped
elif [ "${SWEEPER_EVALUATE_EMPTY_PATCH:-0}" = "1" ]; then
  PHASE=baseline
else
  echo '{"phase": null, "status": "empty_patch", "solved": false, "reward": 0}' >"$LOGS/sweeper-verdict.json"
  finish 0 empty_patch
fi
log "phase=$PHASE harness=$HARNESS model=$MODEL effort=$EFFORT auth=$AUTH"

rm -rf "$OUT" "$EX/temporary" "$EX/secrets"
mkdir -p "$OUT/logs" "$EX/temporary/setup" "$EX/secrets"
cp "$WORKFLOWS" "$EX/workflows.yaml"
RESULT_JSON="$OUT/logs/$PHASE-result.json"
RESULT_JSONL="$OUT/result.jsonl"

envf="$EX/secrets/run.env"
{
  echo "EVALUATION_HARNESS=$HARNESS"
  echo "EVALUATION_MODEL=$MODEL"
  echo "AGENT_BROWSER_MODEL=$MODEL"
  echo "AGENT_BROWSER_PROVIDER=$PROVIDER"
  echo "EVALUATION_CODEX_PROVIDER=$PROVIDER"
  echo "EVALUATION_CODEX_AUTH=$AUTH"
  echo "CODEX_REASONING_EFFORT=$EFFORT"
  echo "CODEX_TIMEOUT_SECONDS=$CODEX_TIMEOUT"
  echo "CODEX_EXECUTABLE=/opt/agent-bin/codex"
  echo "AGENT_BROWSER_WORKFLOW_CATEGORIES=$CATEGORIES"
  echo "AGENT_BROWSER_RECORD=$RECORD"
  echo "BASE_URL=http://127.0.0.1:13200"
  echo "no_proxy=127.0.0.1,localhost"
  echo "NO_PROXY=127.0.0.1,localhost"
} >"$envf"
if [ "$AUTH" = "subscription" ]; then
  if [ -z "${CODEX_AUTH_JSON:-}" ]; then log "SWEEPER_VERIFIER_AUTH=subscription needs CODEX_AUTH_JSON"; exit 1; fi
  printf '%s' "$CODEX_AUTH_JSON" >"$EX/secrets/codex-auth.json"
  chmod 600 "$EX/secrets/codex-auth.json"
  echo "CODEX_AUTH_FILE=$EX/secrets/codex-auth.json" >>"$envf"
else
  if [ -z "${OPENAI_API_KEY:-}${CODEX_API_KEY:-}${ANTHROPIC_API_KEY:-}" ]; then
    log "SWEEPER_VERIFIER_AUTH=api needs OPENAI_API_KEY (or set SWEEPER_VERIFIER_AUTH=subscription + CODEX_AUTH_JSON)"
    exit 1
  fi
  [ -n "${OPENAI_BASE_URL:-}" ] && echo "OPENAI_BASE_URL=$OPENAI_BASE_URL" >>"$envf"
  for key in OPENAI_API_KEY CODEX_API_KEY ANTHROPIC_API_KEY; do
    [ -n "${!key:-}" ] && echo "$key=${!key}" >>"$envf"
  done
fi
chmod 600 "$envf"

product_failed=0
for attempt in 1 2 3; do
  log "phase $PHASE attempt $attempt/3"
  rpc 120 - python3 /evaluation/parse_results.py clear-phase --phase "$PHASE" \
    --result-json "$RESULT_JSON" --result-jsonl "$RESULT_JSONL" --categories "$CATEGORIES" >/dev/null
  rm -f "$EX"/temporary/setup/.setup-*
  if ! reset_workspace "$PATCH"; then
    log "patch does not apply to the base commit"
    exit 1
  fi
  start_product
  if ! rpc 700 - python3 /opt/sweeper/verifier_server.py wait-ready 600 >>"$LOGS/test.log" 2>&1; then
    log "product did not become ready"
    tail -n 80 /tmp/product-dev-server.log >>"$LOGS/test.log" 2>/dev/null || true
    stop_product
    product_failed=1
    break
  fi
  # Each phase attempt gets a fresh CODEX_HOME, like upstream's `docker run --rm`.
  cp "$envf" "$EX/secrets/attempt.env"
  echo "CODEX_HOME=/tmp/codex-home-$attempt-$RANDOM" >>"$EX/secrets/attempt.env"
  rpc 14400 "$EX/secrets/attempt.env" python3 /evaluation/run-workflows.py --phase "$PHASE" \
    --workflows "$EX/workflows.yaml" --output-dir "$OUT" --setup-dir "$EX/temporary/setup" \
    >>"$LOGS/test.log" 2>&1
  rm -f "$EX/secrets/attempt.env"
  stop_product
  uncertain="$(rpc 120 - python3 /evaluation/parse_results.py agent-uncertain --phase "$PHASE" \
    --result-json "$RESULT_JSON" --categories "$CATEGORIES")"
  if [ -n "$uncertain" ] && [ "$attempt" -lt 3 ]; then
    log "agent uncertain: $uncertain; retry phase"
    continue
  fi
  break
done
rm -rf "$EX/secrets"

if [ "$product_failed" = "1" ]; then
  save_logs
  echo "{\"phase\": \"$PHASE\", \"status\": \"product_start_failed\", \"solved\": false}" >"$LOGS/sweeper-verdict.json"
  if [ -n "$PATCH" ]; then
    # The patched app does not start: the patch does not fix the bug.
    finish 0 product_start_failed
  fi
  log "unpatched product did not start: infrastructure failure"
  exit 1
fi

rpc 120 - python3 /opt/sweeper/verifier_server.py summarize "$PHASE" "$RESULT_JSON" \
  "$EX/workflows.yaml" "$EX/verdict.json" >/dev/null
summary_exit=$?
cp "$EX/verdict.json" "$LOGS/sweeper-verdict.json" 2>/dev/null || true
save_logs
cat "$LOGS/sweeper-verdict.json" 2>/dev/null | tee -a "$LOGS/test.log"

if [ "$summary_exit" = "4" ]; then
  log "verifier errors (invalid JSON or exception) after retries: infrastructure failure"
  exit 1
fi
if [ "$summary_exit" != "0" ]; then
  log "verifier cells incomplete: infrastructure failure"
  exit 1
fi
reward="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["reward"])' "$LOGS/sweeper-verdict.json" 2>/dev/null \
  || grep -o '"reward": [01]' "$LOGS/sweeper-verdict.json" | grep -o '[01]$')"
finish "${reward:-0}" verified
