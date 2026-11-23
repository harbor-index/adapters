"""Public-tool gateway between the agent and the EmbodiedLab environment daemon (standard library only, runs as root).

The daemon listens on a Unix socket in a directory only root can enter; the agent (an unprivileged user) reaches the
laboratory only through this gateway on 127.0.0.1.

At start it prepares the trial the way scripts/trial.py does: it waits for the simulator, reads the task specification
and the tool list once, and renders the agent's workspace (README.md, task_card.md, task_spec.json, tools.json, env.json,
the ./py wrapper and the embodiedlab_api client) into the agent's home and hands it to the agent user. It reports
healthy only after that, so the agent starts on a complete workspace.

Then it serves exactly the three Controller routes the agent client (embodiedlab_api) uses and nothing else:
  GET  /v1/tools?environment_id=...        -> the tools of this exposure level
  POST /v1/tools/call {name, arguments}     -> starts one tool call, returns an operation id
  GET  /v1/operations/<id>                  -> the operation (status, output)
The simulation daemon's other routes (truth, exec, evaluate, restore, states, ...) are never forwarded.

`python3 gateway.py capture` (run as root by the verifier's collect hook) seals the episode's evidence.
"""
from __future__ import annotations

import http.client
import json
import os
import pwd
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import render

SIM_UDS = os.environ.get("SIM_UDS", "/run/el/envd.sock")
HOST = os.environ.get("GATEWAY_HOST", "127.0.0.1")
PORT = int(os.environ.get("GATEWAY_PORT", "18731"))
WORKSPACE = os.environ.get("EL_WORKSPACE", "/home/agent/workspace")
AGENT_USER = os.environ.get("EL_AGENT_USER", "agent")
CALL_TIMEOUT_S = float(os.environ.get("GATEWAY_CALL_TIMEOUT_S", "3600"))
OPS: dict[str, dict] = {}
LOCK = threading.Lock()
SIM_LOCK = threading.Lock()   # one tool call at a time, as the daemon itself serializes them
STATE = {"workspace": False, "error": None}


class SimError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"{status}: {detail[:2000]}")
        self.status, self.detail = status, detail


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.unix_path)


def sim(method: str, path: str, body: dict | None = None, timeout: float = 30):
    connection = UnixHTTPConnection(SIM_UDS, timeout)
    try:
        data = json.dumps(body).encode() if body is not None else None
        connection.request(method, path, body=data, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read()
        if response.status >= 400:
            raise SimError(response.status, raw.decode(errors="replace"))
        return json.loads(raw or b"null")
    finally:
        connection.close()


def public_tools() -> list[dict]:
    tools = sim("GET", "/tools", timeout=120)
    tools = tools.get("tools", tools) if isinstance(tools, dict) else tools
    return [{k: t.get(k) for k in ("name", "signature", "description", "effect")} for t in tools]


def call_result(name: str, arguments: dict | None = None):
    with SIM_LOCK:
        output = sim("POST", "/tools/call", {"name": name, "arguments": arguments or {}}, timeout=600)
    if not isinstance(output, dict) or output.get("ok") is False:
        raise RuntimeError(f"{name} failed: {output}")
    return output.get("result")


def prepare() -> None:
    """Wait for the simulator, then render the workspace exactly once (scripts/trial.py start)."""
    try:
        while True:
            try:
                if sim("GET", "/ready", timeout=5).get("ready"):
                    break
            except Exception:
                pass
            time.sleep(5)
        for _ in range(60):
            try:
                call_result("get_robot_state")
                break
            except Exception:
                time.sleep(2)
        task_spec = call_result("get_task_spec")
        render.write_workspace(WORKSPACE, task_spec, public_tools())
        user = pwd.getpwnam(AGENT_USER)
        subprocess.run(["chown", "-R", f"{user.pw_uid}:{user.pw_gid}", WORKSPACE], check=True)
        STATE["workspace"] = True
        print("gateway: workspace ready", flush=True)
    except Exception as exc:
        STATE["error"] = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()


def run_call(op_id: str, name: str, arguments: dict) -> None:
    try:
        with SIM_LOCK:
            output = sim("POST", "/tools/call", {"name": name, "arguments": arguments}, timeout=CALL_TIMEOUT_S)
        update = {"status": "completed", "output": output, "error": None}
    except SimError as exc:
        update = {"status": "failed", "output": None, "error": {"message": f"{exc.status}: {exc.detail[:2000]}"}}
    except Exception as exc:  # transport failure
        update = {"status": "failed", "output": None, "error": {"message": f"{type(exc).__name__}: {exc}"}}
    with LOCK:
        OPS[op_id].update(update)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, value) -> None:
        raw = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):  # one short line per request
        print("gateway:", self.command, self.path.split("?")[0], flush=True)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/healthz":
            ok = STATE["workspace"]
            return self._send(200 if ok else 503, {"ready": ok, "error": STATE["error"]})
        if url.path == "/v1/tools":
            return self._send(200, public_tools())
        if url.path.startswith("/v1/operations/"):
            with LOCK:
                op = OPS.get(url.path.rsplit("/", 1)[-1])
                op = dict(op) if op else None
            return self._send(200, op) if op else self._send(404, {"detail": "no such operation"})
        return self._send(404, {"detail": "not available"})

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        if url.path != "/v1/tools/call":
            return self._send(404, {"detail": "not available"})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        name, arguments = body.get("name"), body.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return self._send(422, {"detail": "name (string) and arguments (object) are required"})
        op_id = "op_" + uuid.uuid4().hex
        with LOCK:
            OPS[op_id] = {"id": op_id, "kind": f"tool.{name}", "status": "running", "output": None, "error": None}
        threading.Thread(target=run_call, args=(op_id, name, arguments), daemon=True).start()
        return self._send(200, {"id": op_id, "status": "running"})


if __name__ == "__main__":
    if sys.argv[1:] == ["capture"]:
        print(json.dumps(sim("POST", "/capture/finalize", timeout=900), default=str)[:2000])
        raise SystemExit(0)
    print(f"gateway on {HOST}:{PORT} -> {SIM_UDS}", flush=True)
    threading.Thread(target=prepare, daemon=True).start()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
