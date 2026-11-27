#!/usr/bin/env bash
# /workspace/task-shell <cmd>: run a product command in the product checkout.
# Upstream (runtime/harnesses/prediction/task_shell.sh) docker-execs
# `bash -lc 'cd /workspace/product && exec "$@"'` in a separate product
# container. In Harbor the product runs in the agent's container, so the same
# login-shell command runs locally. Calls are appended to status.shell.txt like
# upstream.
set -uo pipefail

log_dir=/logs/agent
log="$log_dir/status.shell.txt"
if mkdir -p "$log_dir" 2>/dev/null; then
  { printf '[%s] ' "$(date -Iseconds)"; printf '%q ' "$0" "$@"; echo; } >>"$log" 2>/dev/null || true
fi

bash -lc 'cd /workspace/product && exec "$@"' bash "$@"
code=$?
echo "[exit=$code]" >>"$log" 2>/dev/null || true
exit "$code"
