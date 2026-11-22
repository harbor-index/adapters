"""Local Chat Completions endpoint that answers each request with `codex exec`.

For models that are reachable only through the Codex CLI. Both parity sides
point their shared native policy at this endpoint, so they send the model the
same text. Each request flattens the conversation into one prompt and asks
Codex for the next assistant message; Codex runs in an empty directory with a
read-only sandbox. Codex adds its own system prompt, and the request's
temperature, max_tokens and top_p are not forwarded because the CLI has no such
options. Every call is logged with its token usage and the number of commands
Codex ran, which should be zero.
"""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time

PREAMBLE = (
    "You are being used as a plain chat model. Below is a conversation. Write only "
    "the next assistant message, exactly as the assistant would say it. Do not run "
    "commands, do not use tools, and add nothing outside that message."
)


def flatten(messages):
    parts = [PREAMBLE]
    for message in messages:
        parts.append("[" + message["role"] + "]\n" + message["content"])
    parts.append("[assistant]")
    return "\n\n".join(parts)


class Bridge(ThreadingHTTPServer):
    def __init__(self, address, profile, effort, log_path, timeout, concurrency):
        super().__init__(address, Handler)
        self.profile, self.effort, self.timeout = profile, effort, timeout
        self.log_path = Path(log_path)
        self.lock = threading.Lock()
        self.semaphore = threading.Semaphore(concurrency)

    def record(self, entry):
        with self.lock:
            with self.log_path.open("a") as handle:
                handle.write(json.dumps(entry) + "\n")

    def complete(self, model, messages):
        started = time.time()
        with tempfile.TemporaryDirectory(prefix="codex-bridge-") as work:
            out = Path(work) / "last.txt"
            cwd = Path(work) / "cwd"
            cwd.mkdir()
            command = [
                "codex",
                "exec",
                *(["-p", self.profile] if self.profile else []),
                "-m",
                model,
                "-s",
                "read-only",
                "--skip-git-repo-check",
                "--ephemeral",
                "-c",
                "notify=[]",
                "-c",
                "model_reasoning_effort=" + json.dumps(self.effort),
                "-C",
                str(cwd),
                "-o",
                str(out),
                "--json",
                flatten(messages),
            ]
            with self.semaphore:
                result = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                )
            usage, commands, failure = {}, 0, None
            for line in result.stdout.splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") == "turn.completed":
                    usage = event.get("usage") or {}
                if event.get("type") == "turn.failed":
                    failure = (event.get("error") or {}).get("message")
                if (event.get("item") or {}).get("type") == "command_execution":
                    commands += 1
            text = out.read_text() if out.exists() else ""
        entry = {
            "model": model,
            "messages": len(messages),
            "seconds": round(time.time() - started, 1),
            "return_code": result.returncode,
            "commands_run": commands,
            "failure": failure,
            "usage": usage,
            "reply_chars": len(text),
        }
        self.record(entry)
        if result.returncode != 0 or failure or not text.strip():
            raise RuntimeError(failure or "codex exec produced no reply")
        return text.strip(), usage


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        try:
            if self.path != "/v1/chat/completions":
                raise ValueError("unknown endpoint")
            payload = json.loads(
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
            )
            text, usage = self.server.complete(payload["model"], payload["messages"])
            body = {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": int(usage.get("input_tokens", 0) or 0),
                    "completion_tokens": int(usage.get("output_tokens", 0) or 0),
                    "prompt_tokens_details": {
                        "cached_tokens": int(usage.get("cached_input_tokens", 0) or 0)
                    },
                },
            }
            code = 200
        except Exception as exc:  # report every failure to the caller as HTTP 500
            body, code = {"error": type(exc).__name__ + ": " + str(exc)[:200]}, 500
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile", help="Codex profile; default Codex login if omitted"
    )
    parser.add_argument("--effort", default="medium")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--log", required=True)
    parser.add_argument("--timeout", type=float, default=110)
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    server = Bridge(
        ("127.0.0.1", args.port),
        args.profile,
        args.effort,
        args.log,
        args.timeout,
        args.concurrency,
    )
    print("READY http://127.0.0.1:" + str(server.server_port) + "/v1", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
