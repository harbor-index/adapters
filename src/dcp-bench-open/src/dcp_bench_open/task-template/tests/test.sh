#!/bin/bash
set -euo pipefail
mkdir -p /logs/verifier
printf '0\n' > /logs/verifier/reward.txt
export DOCKER_HOST=unix:///var/run/dcp-verifier.sock
export PYTHONPATH=/tests/native
cleanup() {
  if [ -n "${dcp_daemon_pid:-}" ]; then kill "$dcp_daemon_pid" 2>/dev/null || true; fi
}
trap cleanup EXIT
# No TCP listener, bridge, host socket, or shared Docker data directory.
dind dockerd --host="$DOCKER_HOST" --data-root=/var/lib/dcp-verifier \
  --exec-root=/var/run/dcp-verifier --pidfile=/var/run/dcp-verifier.pid \
  --storage-driver=overlay2 --iptables=false --ip6tables=false --bridge=none \
  --ip-forward=false --ip-masq=false > /logs/verifier/dockerd.log 2>&1 &
dcp_daemon_pid=$!
for attempt in $(seq 1 45); do
  if docker info >/dev/null 2>&1; then break; fi
  if ! kill -0 "$dcp_daemon_pid" 2>/dev/null; then cat /logs/verifier/dockerd.log; exit 2; fi
  sleep 1
done
docker info >/dev/null
python /tests/check_archive.py
docker load --input /tests/solver-image.tar > /logs/verifier/docker-load.log
python /tests/grade.py
