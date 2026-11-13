"""Test process only: serve scripted responses and reject external connections."""

import ipaddress
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).parent
responses = iter(json.loads((ROOT / "responses.json").read_text()))


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with (ROOT / "requests.jsonl").open("a") as out:
            out.write(json.dumps(request) + "\n")
        try:
            message = next(responses)
        except StopIteration:
            self.send_error(500, "Scripted responses exhausted")
            return
        payload = json.dumps(
            {
                "id": "offline-completion",
                "object": "chat.completion",
                "created": 0,
                "model": "offline-model",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls"
                        if message.get("tool_calls")
                        else "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


server = HTTPServer(("127.0.0.1", 18741), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
connect = socket.socket.connect
getaddrinfo = socket.getaddrinfo


def require_loopback(host):
    if host == "localhost":
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    with (ROOT / "blocked-network.txt").open("a") as out:
        out.write(str(host) + "\n")
    raise RuntimeError(f"External network blocked by offline fixture: {host}")


def guarded_connect(sock, address):
    if isinstance(address, tuple):
        require_loopback(address[0])
    return connect(sock, address)


def guarded_getaddrinfo(host, *args, **kwargs):
    require_loopback(host)
    return getaddrinfo(host, *args, **kwargs)


socket.socket.connect = guarded_connect
socket.getaddrinfo = guarded_getaddrinfo
