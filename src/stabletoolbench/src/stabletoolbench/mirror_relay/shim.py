"""Private /virtual HTTP adapter executed inside the trial's trusted broker."""
import base64
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import sys
import time
import uuid

ROOT = Path(__file__).parent
MAX_BYTES = 64 * 1024


def store(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/virtual":
            self.send_error(404)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_BYTES:
                raise ValueError
            body = self.rfile.read(size)
            if not isinstance(json.loads(body), dict):
                raise ValueError
        except (ValueError, UnicodeError):
            self.send_error(400)
            return
        request_id = uuid.uuid4().hex
        waiting = ROOT / (request_id + ".request")
        reply = ROOT / (request_id + ".response")
        store(waiting, body)
        try:
            for _ in range(1500):
                if reply.exists():
                    packet = json.loads(reply.read_bytes())
                    data = json.dumps(packet["data"]).encode()
                    self.send_response(packet["status"])
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                time.sleep(0.1)
            self.send_error(504, "MirrorAPI control-plane timeout")
        finally:
            waiting.unlink(missing_ok=True)
            reply.unlink(missing_ok=True)

    def log_message(self, *args):
        pass


def main():
    command = sys.argv[1]
    if command == "serve":
        os.umask(0o077)
        server = ThreadingHTTPServer(("127.0.0.1", 18180), Handler)
        (ROOT / "ready").touch()
        server.serve_forever()
    elif command == "take":
        for _ in range(40):
            for path in sorted(ROOT.glob("*.request")):
                if re.fullmatch("[0-9a-f]{32}", path.stem) and not path.with_suffix(".response").exists():
                    try:
                        print(json.dumps({"id": path.stem, "body": json.loads(path.read_bytes())}))
                        return
                    except FileNotFoundError:
                        pass
            time.sleep(0.2)
        print("null")
    elif command == "reply":
        request_id, encoded = sys.argv[2:]
        if not re.fullmatch("[0-9a-f]{32}", request_id):
            raise ValueError("Invalid request ID")
        if (ROOT / (request_id + ".request")).is_file():
            store(ROOT / (request_id + ".response"), base64.b64decode(encoded, validate=True))
    else:
        raise ValueError("Invalid action")


if __name__ == "__main__":
    main()
