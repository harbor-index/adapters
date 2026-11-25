#!/usr/bin/env python3
"""Healthy once the environment API answers and the MCP gateway listens."""
import socket
import sys
import urllib.request
from pathlib import Path

try:
    if not Path("/opt/authz/run/ready").is_file():
        sys.exit(1)
    with urllib.request.urlopen("http://127.0.0.1:9100/_env/health", timeout=3) as r:
        if r.status != 200:
            sys.exit(1)
    socket.create_connection(("127.0.0.1", 8000), timeout=3).close()
except Exception:  # noqa: BLE001
    sys.exit(1)
