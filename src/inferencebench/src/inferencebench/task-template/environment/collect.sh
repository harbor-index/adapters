#!/bin/bash
# Derived from aisa-group/InferenceBench src/harbor_adapter/template/environment/collect.sh
# (Apache-2.0). Harbor runs this as root after the agent phase and before it uploads /tests.
# Nothing the agent owns may run as root here: use a clean PATH and PYTHONPATH and leave the
# agent's working directory.
set -uo pipefail
cd /
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PYTHONPATH="/opt/filelock_workarounds/soft_file_locks"
export PYTHONNOUSERSITE=1
unset PYTHONUSERBASE PIP_USER

STATE=/var/lib/ib_state
TASK=/home/agent/task
rm -rf "${STATE}" /tests
mkdir -m 700 "${STATE}" "${STATE}/tmp"
exec > >(tee -a "${STATE}/collect.log") 2>&1
: "${PORT:=8000}" "${INFERENCE_BENCH_SERVER_URL:=http://127.0.0.1:8000}"

phase() {
    echo "[collect] $(date --iso-8601=seconds) $*"
}

# Find the live server first, freeze the agent's processes so they cannot race the writes
# into its task directory, capture the server's environment, and then stop them.
phase "runtime capture"
pid=""
if python3 /opt/inference_eval/bin/server_ready.py "${INFERENCE_BENCH_SERVER_URL}"; then
    pid="$(ss -lptn "sport = :${PORT}" | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' | head -1)"
fi
pkill -STOP -u agent
if [ -n "${pid}" ] && [ -r "/proc/${pid}/environ" ] &&
    python3 /usr/local/bin/capture_runtime_env.py "${pid}" "${TASK}/final_runtime_env.sh" \
        "${TASK}/final_runtime_manifest.txt" "${INFERENCE_BENCH_SERVER_URL}"; then
    chown -h agent:agent "${TASK}/final_runtime_env.sh" "${TASK}/final_runtime_manifest.txt"
    phase "captured the server environment from pid=${pid}"
else
    phase "no live agent server found for runtime capture"
fi
pkill -9 -u agent
sleep 2
phase "stopped the agent's processes"

# The verifier reads and writes these paths as root, and the agent could write to them.
[ ! -L "${TASK}" ] || rm -f "${TASK}"
find /logs/verifier -mindepth 1 -delete
rm -rf /logs/artifacts/collect.log
mkdir -p /logs/artifacts

# As in run_task.sh: keep the /tmp targets of symbolic links in the task directory, and drop
# everything else the agent left in scratch space.
phase "tmp snapshot"
while IFS= read -r link; do
    target="$(readlink "${link}")"
    case "${target}" in
        /tmp/?*) ;;
        *) continue ;;
    esac
    case "${target}" in
        *..*) continue ;;
    esac
    [ -e "${target}" ] || continue
    if mkdir -p "$(dirname "${STATE}${target}")" && cp -aT -- "${target}" "${STATE}${target}"; then
        phase "snapshot ${link} -> ${target} ($(du -sh "${STATE}${target}" | cut -f1))"
    else
        phase "ERROR: snapshot of ${target} failed"
        exit 1
    fi
done < <(find "${TASK}" -maxdepth 1 -type l)
find /tmp /var/tmp /dev/shm -mindepth 1 -user agent -delete 2>/dev/null

# The evaluation server does not get the agent CLI's state or credentials.
rm -rf /home/agent/.nvm /home/agent/.npm /home/agent/.claude /home/agent/.claude.json \
    /home/agent/.codex /home/agent/.gemini /home/agent/.config/opencode \
    /home/agent/.local/share/opencode /home/agent/.local/share/claude
find /home/agent \( -name oauth_token -o -name api_key \) -type f -delete

date --iso-8601=seconds > "${STATE}/complete"
phase "ready for the verifier; /home/agent is $(du -sh /home/agent | cut -f1)"
cp "${STATE}/collect.log" /logs/artifacts/collect.log
