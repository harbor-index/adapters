#!/bin/sh
# The task's environment healthcheck. Harbor runs it as root after the container starts and before the agent is set
# up; Daytona does not run image entrypoints, so the first call starts the trial's two services:
#   envd     the EmbodiedLab environment daemon (Isaac Sim), on a Unix socket that only root can open
#   gateway  the three public tool routes on 127.0.0.1:18731; it also renders the agent's workspace
# Every call exits 0 once the workspace is rendered, non-zero before.
set -eu
T="/opt/el-harbor/tasks/${EL_TASK:?EL_TASK is not set}"
[ -f "$T/el.env" ] || { echo "no such task: $EL_TASK" >&2; exit 1; }
mkdir -p /run/el /var/log/el
chmod 700 /run/el /var/log/el
if mkdir /run/el/started 2>/dev/null; then
  set -a; . "$T/el.env"; set +a
  install -d -m 700 /var/lib/embodiedlab/instances "$EMBODIEDLAB_CAPTURE_DIR"
  cp "$T/instance.json" "/var/lib/embodiedlab/instances/$EMBODIEDLAB_INSTANCE_ID.json"
  chmod 600 "/var/lib/embodiedlab/instances/$EMBODIEDLAB_INSTANCE_ID.json"
  cd /opt/embodiedlab/bench
  setsid nohup /isaac-sim/python.sh -c "import uvicorn
from embodiedlab_sdk.envd import create_env_app
uvicorn.run(create_env_app(), uds='$SIM_UDS', log_level='warning')" >/var/log/el/envd.log 2>&1 </dev/null &
  setsid nohup /usr/bin/python3 /opt/el-harbor/gateway.py >/var/log/el/gateway.log 2>&1 </dev/null &
fi
exec /usr/bin/python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18731/healthz', timeout=5)"
