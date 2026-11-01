"""
Thin proxy between the agent and the game server.

Run this script and feed it one JSON action per line on stdin, e.g.:
    { "id": 1 }
    { "id": 6, "data": { "x": 10, "y": 20 } }

Each line printed back on stdout is the resulting frame data as JSON.
"""

import json
import sys
import time
import urllib.error
import urllib.request

SERVER = "http://game:8000"


def get(path):
    with urllib.request.urlopen(f"{SERVER}{path}") as r:
        return json.loads(r.read())


def post(path, data):
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        f"{SERVER}{path}",
        data=body,
        headers={ "Content-Type": "application/json" },
    )

    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


# Wait for the game server to be ready
for _ in range(300):
    try:
        get("/health")
        break
    except urllib.error.URLError:
        time.sleep(1)

print(json.dumps(get("/observation")), flush=True)

for line in sys.stdin:
    line = line.strip()

    if not line:
        continue

    try:
        action = json.loads(line)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"invalid JSON: {e}"}), flush=True)
        continue

    try:
        frame = post("/action", action)
    except urllib.error.HTTPError as e:
        print(json.dumps({"error": e.read().decode()}), flush=True)
        continue

    print(json.dumps(frame), flush=True)
