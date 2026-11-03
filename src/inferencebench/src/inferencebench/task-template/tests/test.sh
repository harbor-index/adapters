#!/bin/bash
# Derived from aisa-group/InferenceBench src/harbor_adapter/template/tests/test.sh (Apache-2.0).
# Runs as root in the agent's sandbox after collect.sh has stopped the agent's processes.
# Nothing the agent owns may run as root here: resolve commands through a clean PATH, and call
# tools by absolute path in `env -i`, which searches the PATH it was given.
# The evaluation server gets upstream's PATH order, with the agent's bin directory first.
AGENT_PATH="/root/.local/bin:/home/agent/.local/bin:${PATH}"
IMAGE_PYTHONPATH="${PYTHONPATH-}"
cd /
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PYTHONPATH="/opt/filelock_workarounds/soft_file_locks"
export PYTHONNOUSERSITE=1
unset PYTHONUSERBASE PIP_USER

IB=/tests/ib
STATE=/var/lib/ib_state
JUDGE_HOME=/var/lib/ib_judge
LOGS=/logs/verifier
TASK=/home/agent/task
EVAL_LOG="${LOGS}/eval.log"
SCENARIO="${INFERENCE_BENCH_SCENARIO}"
STUB_MARKER="ERROR: start_server.sh has no engine configured"
SERVER_PID=""
FINAL_EVAL_RC=1
JUDGE_RC=1
# Upstream has no limit on an evaluation attempt, and a slow but valid server needs hours:
# scenario B sends 64 sequential requests of about 7400 tokens, so vLLM's default 18 ms per
# token takes 2.4 h. Stop early enough to write a reward before Harbor's 12-hour verifier limit.
EVAL_DEADLINE=$(( $(date +%s) + 41400 ))
: "${PORT:=8000}" "${INFERENCE_BENCH_SERVER_URL:=http://127.0.0.1:8000}" "${INFERENCE_BENCH_SERVER_WAIT_S:=900}"
chmod 700 /tests
mkdir -p "${LOGS}" /root/eval_home /root/eval_tmp
chmod 755 "${LOGS}"
source "${IB}/src/disallowed_usage_judge/judge.sh"

log() {
    echo "[${SCENARIO}] $(date --iso-8601=seconds) $*" | tee -a "${EVAL_LOG}"
}

write_error_metrics() {
    python3 - "${LOGS}/metrics.json" "${SCENARIO}" "${INFERENCE_BENCH_BASE_MODEL}" "$1" <<'PY'
import json
import sys
from pathlib import Path
path, scenario, model, error = sys.argv[1:]
Path(path).write_text(json.dumps({
    "scenario": scenario, "model_id": model, "profiles": {}, "vram_peak_mb": 0.0, "error": error,
}, indent=2), encoding="utf-8")
PY
}

link_eval_samples() {
    local baselines="${IB}/src/eval/inference/baselines"
    local samples=/opt/inference_eval/baselines/samples
    local eval_dir="${baselines}/samples/longbench_v2/${INFERENCE_BENCH_DATASET_SEED}_${INFERENCE_BENCH_LONGBENCH_N}"
    mkdir -p "${eval_dir}" "${baselines}/samples/mmlu_pro"
    ln -sfn "${samples}/longbench_v2/${INFERENCE_BENCH_QUALITY_SEED}_${INFERENCE_BENCH_LONGBENCH_N}/samples.jsonl" "${eval_dir}/samples.jsonl"
    ln -sfn "${samples}/mmlu_pro/${INFERENCE_BENCH_QUALITY_SEED}_${INFERENCE_BENCH_QUALITY_MMLUPRO_N}" \
        "${baselines}/samples/mmlu_pro/${INFERENCE_BENCH_QUALITY_SEED}_${INFERENCE_BENCH_QUALITY_MMLUPRO_N}"
    ln -sfn /opt/inference_eval/baselines/quality "${baselines}/quality"
}

restore_tmp() {
    local entry
    for entry in "${STATE}"/tmp/*; do
        [ -e "${entry}" ] || continue
        rm -rf "/tmp/$(basename "${entry}")"
        cp -a "${entry}" /tmp/ && chown -R agent:agent "/tmp/$(basename "${entry}")"
    done
}

agent_env() {
    local names=(HF_HOME HF_HUB_CACHE HUGGINGFACE_HUB_CACHE TRANSFORMERS_CACHE TMPDIR TMP TEMP XDG_CACHE_HOME
        VLLM_TORCH_COMPILE_CACHE_DIR TORCHINDUCTOR_CACHE_DIR TORCH_EXTENSIONS_DIR TRITON_CACHE_DIR
        PIP_CACHE_DIR SOFT_FILELOCK NO_PROXY no_proxy LD_LIBRARY_PATH HOST PORT INFERENCE_BENCH_SERVER_URL
        INFERENCE_BENCH_SERVER_WAIT_S INFERENCE_BENCH_BASE_MODEL INFERENCE_BENCH_MAX_MODEL_LEN)
    local name
    AGENT_ENV=(HOME=/home/agent "PATH=${AGENT_PATH}" "PYTHONPATH=${IMAGE_PYTHONPATH}" PIP_USER=1
        PYTHONUSERBASE=/home/agent/.local)
    for name in "${names[@]}" $(compgen -e | grep -E '^(NVIDIA_|CUDA_|RAY_)'); do
        [ -z "${!name+x}" ] || AGENT_ENV+=("${name}=${!name}")
    done
}

port_listeners() {
    ss -lptn "sport = :${PORT}" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2
}

# The judge reviews a copy of the task directory, as condor's judge reviews a read-only mount.
# The real home directory is closed to the agent user until the judge has finished.
run_judge() {
    if [ -z "${ANTHROPIC_API_KEY:-}${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
        echo "No judge credential; skipping the disallowed-usage judge."
        return
    fi
    chown root:root /home/agent
    chmod 700 /home/agent
    rm -rf "${JUDGE_HOME}"
    mkdir -p "${JUDGE_HOME}"
    cp -a "${TASK}" "${JUDGE_HOME}/task"
    chown -R agent:agent "${JUDGE_HOME}"
    local prompt
    prompt="$(python3 "${IB}/src/disallowed_usage_judge/get_judge_prompt.py" \
        --benchmark "$(cat "${IB}/src/eval/tasks/${SCENARIO}/benchmark.txt")" \
        --model "${INFERENCE_BENCH_BASE_MODEL}" --task-dir "${JUDGE_HOME}/task")"
    local credential="ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}"
    [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] || credential="CLAUDE_CODE_OAUTH_TOKEN=${CLAUDE_CODE_OAUTH_TOKEN}"
    local base_url=()
    [ -z "${ANTHROPIC_BASE_URL:-}" ] || base_url=("ANTHROPIC_BASE_URL=${ANTHROPIC_BASE_URL}")
    (
        cd "${JUDGE_HOME}" || exit 1
        /usr/bin/env -i "${AGENT_ENV[@]}" "HOME=${JUDGE_HOME}" "${credential}" "${base_url[@]}" \
            /usr/bin/timeout --signal=TERM --kill-after=30s 15m \
            /usr/bin/setpriv --reuid=agent --regid=agent --clear-groups \
            claude --print --dangerously-skip-permissions --model "${DEFAULT_JUDGE_MODEL}" "${prompt}" < /dev/null
    ) > "${LOGS}/judge.log" 2>&1
    JUDGE_RC=$?
    pkill -9 -u agent
    valid_judgements "${JUDGE_HOME}" || JUDGE_RC=1
    for f in contamination_judgement.txt disallowed_model_judgement.txt; do
        if [ -f "${JUDGE_HOME}/${f}" ] && [ ! -L "${JUDGE_HOME}/${f}" ]; then
            cp "${JUDGE_HOME}/${f}" "${LOGS}/${f}"
        else
            echo "[warn] ${f} was not produced."
        fi
    done
    rm -rf "${JUDGE_HOME}"
    find /tmp /var/tmp /dev/shm -mindepth 1 -user agent -delete 2>/dev/null
    chown agent:agent /home/agent
    chmod 755 /home/agent
    echo "Disallowed-usage judge exit code: ${JUDGE_RC}"
}

server_ready() {
    python3 /opt/inference_eval/bin/server_ready.py "${INFERENCE_BENCH_SERVER_URL}"
}

stop_server() {
    if [ -n "${SERVER_PID}" ]; then
        kill -- "-${SERVER_PID}" 2>/dev/null
        sleep 2
        kill -9 -- "-${SERVER_PID}" 2>/dev/null
        SERVER_PID=""
    fi
    local pid
    for pid in $(port_listeners); do
        kill "${pid}" 2>/dev/null
        sleep 2
        kill -9 "${pid}" 2>/dev/null
    done
}

start_server() {
    stop_server
    local wait_s=0
    while [ "${wait_s}" -lt 30 ] && [ -n "$(port_listeners)" ]; do
        sleep 1
        wait_s=$((wait_s + 1))
    done
    restore_tmp
    local launch=""
    local f
    for f in start_server.sh start_server_original.sh; do
        if [ -f "${TASK}/${f}" ] && ! grep -q "${STUB_MARKER}" "${TASK}/${f}"; then
            launch="${TASK}/${f}"
            break
        fi
    done
    if [ -z "${launch}" ]; then
        log "no configured start_server.sh found for the eval server."
        return 1
    fi
    log "starting the eval server via ${launch}"
    cd "${TASK}" || return 1
    /usr/bin/env -i "${AGENT_ENV[@]}" /usr/bin/setsid /usr/bin/setpriv --reuid=agent --regid=agent --clear-groups \
        /bin/bash -c 'set -euo pipefail; if [ -f /home/agent/task/final_runtime_env.sh ]; then . /home/agent/task/final_runtime_env.sh; fi; exec /opt/inference_eval/bin/launch_supervised_server.sh "$1"' \
        _ "${launch}" >> "${LOGS}/server.log" 2>&1 &
    SERVER_PID=$!
    cd "${IB}" || return 1
    local deadline=$(( $(date +%s) + INFERENCE_BENCH_SERVER_WAIT_S ))
    while [ "$(date +%s)" -lt "${deadline}" ]; do
        if server_ready; then
            log "eval server is ready at ${INFERENCE_BENCH_SERVER_URL}"
            return 0
        fi
        if ! kill -0 "${SERVER_PID}" 2>/dev/null; then
            log "eval server exited before becoming ready"
            return 1
        fi
        sleep 1
    done
    log "eval server did not become ready within ${INFERENCE_BENCH_SERVER_WAIT_S}s"
    return 1
}

run_eval_attempt() {
    local left=$(( EVAL_DEADLINE - $(date +%s) ))
    HOME=/root/eval_home TMPDIR=/root/eval_tmp XDG_CACHE_HOME=/root/eval_home/.cache \
        INFERENCE_BENCH_ALLOW_HF_DOWNLOAD=0 timeout --signal=TERM --kill-after=60s "${left}" \
        python3 "src/eval/tasks/${SCENARIO}/evaluate.py" \
        --server-url "${INFERENCE_BENCH_SERVER_URL}" \
        --json-output-file "${LOGS}/metrics.json" \
        --quality-tau "${INFERENCE_BENCH_QUALITY_TAU}" \
        "$@" >> "${EVAL_LOG}" 2>&1
}

run_eval_with_retry() {
    local max_retries="$1"
    shift
    local attempt
    for ((attempt = 1; attempt <= max_retries; attempt++)); do
        [ "$(date +%s)" -lt "${EVAL_DEADLINE}" ] || return 1
        if ! server_ready && ! start_server; then
            FINAL_EVAL_RC=1
            continue
        fi
        log "eval attempt ${attempt}/${max_retries} $*"
        FINAL_EVAL_RC=0
        run_eval_attempt "$@" || FINAL_EVAL_RC=$?
        if [ "${FINAL_EVAL_RC}" -eq 0 ] && python3 /tests/valid_eval_metrics.py "${LOGS}/metrics.json" "${IB}"; then
            return 0
        fi
        [ "${FINAL_EVAL_RC}" -ne 0 ] || FINAL_EVAL_RC=1
        log "attempt ${attempt} failed (rc=${FINAL_EVAL_RC})"
    done
    return 1
}

echo "=== InferenceBench verifier: ${SCENARIO} ${INFERENCE_BENCH_BASE_MODEL} eval_seed=${INFERENCE_BENCH_DATASET_SEED} ==="
if [ ! -f "${STATE}/complete" ]; then
    echo "[fatal] collect.sh did not finish; no reward written, so Harbor records an infrastructure error"
    exit 1
fi

agent_env
link_eval_samples
log "judge start"
run_judge
log "judge done (rc=${JUDGE_RC})"

unset ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN HF_TOKEN
for f in metrics_preview.json server.log final_runtime_env.sh final_runtime_manifest.txt; do
    [ ! -f "${TASK}/${f}" ] || [ -L "${TASK}/${f}" ] || cp "${TASK}/${f}" "${LOGS}/${f}"
done
python3 /tests/snapshot.py "${TASK}" "${LOGS}/task"

cd "${IB}" || exit 0
rm -f "${LOGS}/metrics.json"
if start_server; then
    log "starting final evaluation (speed + quality)"
    if ! run_eval_with_retry 3; then
        log "phase 1 failed; retrying with reduced request-timeout"
        run_eval_with_retry 2 --request-timeout-s 150
    fi
else
    log "final evaluation aborted: inference server unavailable"
    write_error_metrics "inference server unavailable before final eval at ${INFERENCE_BENCH_SERVER_URL}"
fi
log "final eval exit code: ${FINAL_EVAL_RC}"
stop_server
# Writes /logs/verifier/reward.json.
python3 /tests/write_reward.py "${LOGS}" "${SCENARIO}" "${FINAL_EVAL_RC}" "${JUDGE_RC}"
exit 0
