"""Run the published website and browser in a separate trusted service."""
import argparse
import asyncio
import base64
import hashlib
from datetime import datetime
import json
import os
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from action_input import validate_action
from playwright_controller import PlaywrightController
from web_actions import ACTION_SPACE

class Session:
    def __init__(self, query, directory, frontend_port, backend_port):
        self.query = query
        self.directory = directory
        self.frontend = f"http://localhost:{frontend_port}"
        self.backend = f"http://localhost:{backend_port}"
        self.controller = PlaywrightController(headless=True)
        self.hashes = {}
        self.steps = 0
        self.finished = False
        self.lock = asyncio.Lock()
        self.started = time.monotonic()

    async def start(self):
        self.directory.mkdir(parents=True, exist_ok=False)
        await self.controller.initialize()
        # Only the website may supply browser content. Agent actions stay on its frontend.
        async def website_only(route):
            url = urlsplit(route.request.url)
            if url.scheme == "http" and ("http://" + url.netloc) in (self.frontend, self.backend):
                await route.continue_()
            else:
                await route.abort()
        await self.controller.context.route("**/*", website_only)
        await self.controller.page.goto(self.frontend, wait_until="networkidle")
        await asyncio.sleep(2)
        self.started = time.monotonic()
        await self.record(None, None)

    async def observation(self, with_screen=False):
        return await self.controller.get_observation(include_screenshot=with_screen, include_dom=True)

    def save_blob(self, name, data):
        (self.directory / name).write_bytes(data)
        self.hashes[name] = hashlib.sha256(data).hexdigest()

    def save_status(self, phase):
        status = {"phase": phase, "steps": self.steps, "files": self.hashes}
        temp = self.directory / "capture.tmp"
        temp.write_text(json.dumps(status))
        temp.replace(self.directory / "capture.json")

    async def record(self, action, result):
        kind = action["action_type"] if action else None
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        if kind != "FAIL":
            obs = await self.observation(with_screen=True)
            prefix = "initial" if kind is None else "final" if kind == "DONE" else "step"
            stem = f"{prefix}_{self.steps}_{stamp}"
            raw = obs["raw_dom"].encode()
            self.save_blob(stem + "_raw.html", raw)
            self.save_blob(stem + ".png", base64.b64decode(obs["screenshot"], validate=True))
            if kind is None:
                # Supply the initial live page only for a Harbor trial with zero actions.
                self.save_blob("final_0_nop_raw.html", raw)
                entry = {"state":"initial", "task":self.query, "start_url":self.frontend,
                         "page_info":obs["page_info"]}
            elif kind == "DONE":
                entry = {"action":action, "state":"done"}
            else:
                entry = {"action":action, "result":result, "page_info":obs["page_info"]}
            entry.update(step_num=self.steps, timestamp=datetime.now().isoformat())
            path = self.directory / "traj.jsonl"
            with path.open("a") as out:
                out.write(json.dumps(entry) + "\n")
            self.hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        reason = "done" if kind == "DONE" else "fail" if kind == "FAIL" else "active"
        summary = {"query": self.query, "steps": self.steps, "execution_time": time.monotonic()-self.started,
                   "reason":reason, "success":kind == "DONE"}
        self.save_blob("result.json", json.dumps(summary).encode())
        self.save_status("complete")

    async def call(self, path, body):
        if path == "/ready":
            return {"ready": True}
        async with self.lock:
            if path == "/actions":
                return [{"action_type": spec["action_type"], "parameters": {
                    name: value.get("description", "") for name, value in spec.get("parameters", {}).items()}}
                    for spec in ACTION_SPACE]
            if path == "/observe":
                obs = await self.observation()
                return {"page_info": obs["page_info"], "dom": obs["dom"], "steps": self.steps}
            if path == "/screenshot":
                return {"screenshot": await self.controller.get_screenshot()}
            if path != "/act":
                raise ValueError("Unsupported request")
            action = validate_action(body, self.frontend)
            if self.finished or self.steps >= 100:
                raise ValueError("Session is complete")
            # Commit a pending marker before any state change, including the browser action.
            self.save_status("pending")
            if self.steps == 0:
                (self.directory / "final_0_nop_raw.html").unlink()
                self.hashes.pop("final_0_nop_raw.html")
            self.steps += 1
            try:
                result = await self.controller.execute_action(action)
                self.finished = action["action_type"] in {"DONE", "FAIL"}
                if not self.finished:
                    await asyncio.sleep(1)
                await self.record(action, result)
            except BaseException:
                self.finished = True
                self.save_status("error")
                raise
            return result

async def main(opts):
    # Preserve the published handler and bind the browser-only API to loopback.
    original = (opts.site / "backend/server.js").read_text()
    if original.count("app.listen(PORT, () => {") != 1:
        raise ValueError("Unrecognized Note backend listener")
    code = original.replace("app.listen(PORT, () => {", f"app.listen({opts.backend_port}, '127.0.0.1', () => {{")
    backend = subprocess.Popen(["node", "-e", code], cwd=opts.site / "backend")
    front_env = dict(os.environ, PORT=str(opts.frontend_port), HOST="127.0.0.1", BROWSER="none",
                     REACT_APP_API_URL=f"http://localhost:{opts.backend_port}", CI="true")
    frontend = subprocess.Popen(["node", "node_modules/react-scripts/scripts/start.js"],
                                cwd=opts.site / "frontend", env=front_env)
    session = Session(json.loads(opts.query.read_text())["query"], opts.evidence, opts.frontend_port, opts.backend_port)
    loop = asyncio.get_running_loop()
    class API(BaseHTTPRequestHandler):
        def reply(self):
            try:
                n = int(self.headers.get("Content-Length", 0))
                if not 0 <= n <= 65536:
                    raise ValueError("Invalid request size")
                if (self.command == "POST") != (self.path == "/act"):
                    raise ValueError("Incorrect request method")
                body = json.loads(self.rfile.read(n)) if n else None
                result = asyncio.run_coroutine_threadsafe(session.call(self.path, body), loop).result(120)
                status = HTTPStatus.OK
            except (ValueError, KeyError) as error:
                status, result = HTTPStatus.BAD_REQUEST, {"error": str(error)}
            except Exception as error:
                status, result = HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)}
            data = json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        do_POST = do_GET = reply
    try:
        for attempt in range(100):
            if backend.poll() is not None:
                raise RuntimeError("Note backend exited")
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", opts.backend_port)
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                await asyncio.sleep(.1)
        else:
            raise RuntimeError("Note backend unavailable")
        # Wait for the dev server to finish compiling. CRA opens its port first.
        from urllib.request import urlopen
        for attempt in range(180):
            if frontend.poll() is not None:
                raise RuntimeError("Note frontend exited")
            try:
                def frontend_ready():
                    with urlopen(session.frontend, timeout=2) as response:
                        return b'id="root"' in response.read()
                if await asyncio.to_thread(frontend_ready):
                    break
            except OSError:
                pass
            await asyncio.sleep(1)
        else:
            raise RuntimeError("Note frontend unavailable")
        await session.start()
        api = ThreadingHTTPServer((opts.bind, opts.control_port), API)
        threading.Thread(target=api.serve_forever, daemon=True).start()
        await asyncio.Future()
    finally:
        backend.terminate()
        backend.wait(timeout=10)
        frontend.terminate()
        frontend.wait(timeout=10)
        await session.controller.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", type=Path, default=Path("/opt/site"))
    parser.add_argument("--query", type=Path, default=Path(__file__).with_name("query.json"))
    parser.add_argument("--evidence", type=Path, default=Path("/evidence"))
    parser.add_argument("--frontend-port", type=int, default=3000)
    parser.add_argument("--backend-port", type=int, default=5100)
    parser.add_argument("--control-port", type=int, default=8765)
    parser.add_argument("--bind", default="0.0.0.0")
    asyncio.run(main(parser.parse_args()))
