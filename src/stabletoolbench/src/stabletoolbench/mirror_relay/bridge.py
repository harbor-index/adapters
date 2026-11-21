"""Forward only selected StableToolBench calls to the local fixed simulator."""
import asyncio
import base64
from datetime import datetime, timezone
from http.client import HTTPException
import json
from pathlib import Path
import re
import shlex
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
import uuid

ENDPOINT = "http://127.0.0.1:18180/virtual"
FIELDS = {"category", "tool_name", "api_name", "tool_input", "strip", "toolbench_key"}
REMOTE_START = """
import base64, os, pathlib, subprocess, sys, time
base = pathlib.Path(sys.argv[1])
base.mkdir(mode=0o700)
script = base / "shim.py"
script.write_bytes(base64.b64decode(sys.argv[2], validate=True))
with open(base / "server.log", "wb") as out:
    proc = subprocess.Popen([sys.executable, str(script), "serve"], stdin=subprocess.DEVNULL,
                            stdout=out, stderr=out, start_new_session=True)
for _ in range(100):
    if proc.poll() is not None:
        raise RuntimeError("Broker HTTP shim exited on startup")
    if (base / "ready").exists():
        break
    time.sleep(0.05)
else:
    raise TimeoutError("Broker HTTP shim not ready")
"""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, *args):
        return None


def simulate(body, allowlist):
    if not isinstance(body, dict) or set(body) != FIELDS or body["toolbench_key"] != "":
        return {"status": 400, "data": {"error": "Invalid tool request", "response": ""}}
    identity = tuple(body.get(k) for k in ("category", "tool_name", "api_name"))
    if not all(isinstance(v, str) for v in identity) or identity not in allowlist:
        return {"status": 403, "data": {"error": "Tool is outside this task", "response": ""}}
    # The upstream simulator parses the argument string, including malformed
    # JSON. Preserve it so a native tool error remains a normal observation.
    if not isinstance(body["tool_input"], (str, dict)) or not isinstance(body["strip"], str):
        return {"status": 400, "data": {"error": "Invalid tool request field", "response": ""}}
    payload = json.dumps(body).encode()
    if len(payload) > 65536:
        return {"status": 413, "data": {"error": "Oversized tool request", "response": ""}}
    client = build_opener(ProxyHandler({}), NoRedirect())
    request = Request(ENDPOINT, data=payload, headers={"Content-Type": "application/json"})
    try:
        with client.open(request, timeout=110) as response:
            data = response.read(65537)
            if len(data) > 65536:
                raise ValueError("Oversized simulator response")
            result = json.loads(data)
            if not isinstance(result, dict) or not {"error","response"}.issubset(result):
                raise ValueError("Unexpected simulator response")
            return {"status": response.status, "data": result}
    except (OSError, ValueError, HTTPException):
        return {"status": 502, "data": {"error": "MirrorAPI infrastructure error", "response": ""}}


class Bridge:
    def __init__(self, run_in_broker, allowed, log_path):
        self.execute = run_in_broker
        self.allowed = {tuple(row[k] for k in ("category", "tool_name", "api_name")) for row in allowed}
        if not self.allowed:
            raise ValueError("Empty trial tool allowlist")
        self.remote_dir = "/tmp/stb-mirror-" + uuid.uuid4().hex
        self.log_path = Path(log_path)
        self.known = {}

    async def start(self):
        source = base64.b64encode(Path(__file__).with_name("shim.py").read_bytes()).decode()
        await self.execute(shlex.join(["python3", "-c", REMOTE_START, self.remote_dir, source]))

    async def step(self):
        script = self.remote_dir + "/shim.py"
        packet = json.loads(await self.execute(shlex.join(["python3", script, "take"])))
        if packet is None:
            return False
        request_id = packet["id"]
        if not isinstance(request_id, str) or not re.fullmatch("[a-f0-9]{32}", request_id):
            raise ValueError("Invalid request ID")
        if request_id not in self.known:
            result = await asyncio.to_thread(simulate, packet["body"], self.allowed)
            self.known[request_id] = result
            evidence = {"utc": datetime.now(timezone.utc).isoformat(), "id": request_id,
                        "request": packet["body"], "result": result}
            with self.log_path.open("a") as out:
                out.write(json.dumps(evidence, ensure_ascii=True) + "\n")
        encoded = base64.b64encode(json.dumps(self.known[request_id]).encode()).decode()
        await self.execute(shlex.join(["python3", script, "reply", request_id, encoded]))
        return True

    async def run(self):
        while True:
            await self.step()
