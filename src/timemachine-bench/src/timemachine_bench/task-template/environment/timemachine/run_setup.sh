#!/bin/bash
# Runs the upstream setup script with pip pointed at the date-filtered index,
# as the upstream Dockerfile does with
#   ENV PIP_INDEX_URL=http://localhost:5000/snapshot/<date>
#   ENV PIP_TRUSTED_HOST=localhost
#   RUN bash /work/setup_<repo>.sh
# The index variables are set only for this step, so the final image does not
# point pip at a server that is no longer running.
set -uo pipefail

script="$1"
date="$2"
log=/tmp/pypi-timemachine.log

/opt/pypi-timemachine/venv/bin/pypi-timemachine --port 5000 "${date}" >"${log}" 2>&1 &
server=$!

ready=0
for _ in $(seq 1 120); do
  if curl -fsS -o /dev/null "http://localhost:5000/snapshot/${date}/pip/"; then
    ready=1
    break
  fi
  sleep 1
done
if [ "${ready}" -ne 1 ]; then
  echo "pypi-timemachine did not become ready" >&2
  cat "${log}" >&2
  exit 1
fi

cd /work
# The index server streams every file from PyPI, and project pages with many
# files (e.g. grpcio) can take longer than pip's 15 s default to filter. A
# timed-out page makes pip backtrack to other versions instead of failing, so
# the network limits are raised; they do not change how pip resolves.
PIP_INDEX_URL="http://localhost:5000/snapshot/${date}" \
PIP_TRUSTED_HOST=localhost \
PIP_DEFAULT_TIMEOUT=300 \
PIP_RETRIES=10 \
POETRY_REQUESTS_TIMEOUT=300 \
  bash "/work/${script}"
status=$?

kill "${server}" 2>/dev/null
wait "${server}" 2>/dev/null
if [ "${status}" -ne 0 ]; then
  echo "setup script exited with status ${status}; last index server log lines:" >&2
  tail -n 50 "${log}" >&2
fi
rm -f "${log}"
exit "${status}"
