"""
ARC-AGI-3 game.

Runs the actual game object and provides an HTTP API so the agent
container can play the game without being able to write the result.

Endpoints:
  GET  /health       - healthcheck
  GET  /observation  - current frame data
  POST /action       - perform one action and returns frame data
  GET  /result       - current state (requires RESULT_TOKEN)
"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

from arcengine import ActionInput
from __MODULE__ import __CLASS_NAME__

PORT = 8000
RESULT_TOKEN = "{result_token}"

_game = __CLASS_NAME__()
_frame = _game.perform_action(ActionInput(id=0))  # RESET: establishes the first frame


def _serialize(frame_data):
    return {
        "game_id": frame_data.game_id,
        "frame": frame_data.frame,
        "state": frame_data.state.value,
        "levels_completed": frame_data.levels_completed,
        "win_levels": frame_data.win_levels,
        "available_actions": frame_data.available_actions,
    }


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/health":
            self._json(200, { "ok": True })
        elif path == "/observation":
            self._json(200, _serialize(_frame))
        elif path == "/result":
            if self._token() != RESULT_TOKEN:
                self._json(401, { "error": "unauthorized" })
                return

            self._json(
                200,
                {
                    "state": _frame.state.value,
                    "levels_completed": _frame.levels_completed,
                    "win_levels": _frame.win_levels,
                },
            )
        else:
            self._json(404, { "error": "not found" })

    def do_POST(self):
        global _frame

        if self.path != "/action":
            self._json(404, { "error": "not found" })
            return

        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        try:
            action = ActionInput(**body)
        except Exception as e:
            self._json(400, { "error": str(e) })
            return

        try:
            _frame = _game.perform_action(action)
        except Exception as e:
            self._json(400, { "error": str(e) })
            return

        self._json(200, _serialize(_frame))

    def _token(self):
        qs = self.path.split("?")[1] if "?" in self.path else ""
        for part in qs.split("&"):
            if part.startswith("token="):
                return part[len("token="):]

        return ""

    def _json(self, code, data):
        body = json.dumps(data, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    print(f"game_server: __MODULE__ ready on :{PORT}", flush=True)
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
