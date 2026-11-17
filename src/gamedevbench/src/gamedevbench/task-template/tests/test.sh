#!/usr/bin/env bash
set -euo pipefail
unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY no_proxy NO_PROXY
mkdir -p /logs/verifier
printf '0\n' > /logs/verifier/reward.txt
exec /opt/gamedevbench/.venv/bin/python /tests/verify.py
