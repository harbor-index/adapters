"""A fake of the OpenAI Chat Completions endpoint, for the CPU mock only.

It lets the real ``DirectAgent`` and the real LiteLLM client run over HTTP in
the OpenAI wire format without a key, a network or any cost. It checks every
request the way the real endpoint would reject a malformed one, then answers
with a tool call planned by the scripted mock solver from the head-camera
picture in the prompt.

It is not a model and knows only the mock scene. It shows that the request and
response formats fit, not that any model can read the pictures.
"""

from __future__ import annotations

import argparse
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import time
from typing import Any

from ..direct.contract import TOOL_NAME
from . import solver

SERVED_MODEL = "fake-vision-model-wire-test"
PROMPT_TOKENS, COMPLETION_TOKENS = 1200, 150
PNG_PREFIX = "data:image/png;base64,"
# Parameters the real endpoint accepts; anything else would be rejected there.
ALLOWED_PARAMETERS = {
    "model", "messages", "tools", "tool_choice", "parallel_tool_calls", "temperature",
    "max_tokens", "max_completion_tokens", "seed", "reasoning_effort", "stream", "n", "user",
}


class RequestError(ValueError):
    """The request would be rejected by the real endpoint."""


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise RequestError(message)


def check_request(body: Any) -> None:
    """Validate a Chat Completions request for the parts this adapter uses."""
    _expect(isinstance(body, dict), "The request body must be a JSON object")
    unknown = sorted(set(body) - ALLOWED_PARAMETERS)
    _expect(not unknown, f"Unrecognized request arguments: {', '.join(unknown)}")
    _expect(isinstance(body.get("model"), str) and body["model"], "model is required")
    _expect(not body.get("stream"), "This fake does not stream")
    tools = body.get("tools")
    _expect(isinstance(tools, list) and len(tools) == 1, "Exactly one tool is expected")
    function = tools[0].get("function") if isinstance(tools[0], dict) else None
    _expect(
        tools[0].get("type") == "function" and isinstance(function, dict)
        and function.get("name") == TOOL_NAME and isinstance(function.get("parameters"), dict)
        and function["parameters"].get("type") == "object",
        f"The tool must be the function {TOOL_NAME} with an object schema",
    )
    _expect(body.get("tool_choice") in ("required", "auto"), "tool_choice must be 'required' or 'auto'")
    if "parallel_tool_calls" in body:
        _expect(type(body["parallel_tool_calls"]) is bool, "parallel_tool_calls must be a boolean")
    messages = body.get("messages")
    _expect(isinstance(messages, list) and len(messages) >= 2, "messages must hold a system and a user turn")
    _expect(messages[0].get("role") == "system" and isinstance(messages[0].get("content"), str),
            "The first message must be a text system message")
    pending: list[str] = []  # Tool calls that still need their tool message.
    for index, message in enumerate(messages[1:], 1):
        role, content = message.get("role"), message.get("content")
        if role == "tool":
            _expect(bool(pending) and message.get("tool_call_id") == pending[0],
                    f"messages[{index}]: tool message does not answer the preceding tool call")
            _expect(isinstance(content, str), f"messages[{index}]: tool content must be text")
            pending.pop(0)
            continue
        _expect(not pending, f"messages[{index}]: an assistant tool call has no tool message")
        if role == "assistant":
            calls = message.get("tool_calls")
            if calls is None:
                _expect(isinstance(content, str) and content, f"messages[{index}]: empty assistant message")
                continue
            _expect(isinstance(calls, list) and calls, f"messages[{index}]: tool_calls must be a list")
            for call in calls:
                called = call.get("function") if isinstance(call, dict) else None
                _expect(
                    isinstance(called, dict) and call.get("type") == "function"
                    and isinstance(call.get("id"), str) and isinstance(called.get("name"), str)
                    and isinstance(called.get("arguments"), str),
                    f"messages[{index}]: malformed tool call",
                )
                pending.append(call["id"])
        elif role == "user":
            if isinstance(content, str):
                continue
            _expect(isinstance(content, list) and content, f"messages[{index}]: empty user message")
            for part in content:
                kind = part.get("type") if isinstance(part, dict) else None
                if kind == "text":
                    _expect(isinstance(part.get("text"), str), f"messages[{index}]: text part without text")
                elif kind == "image_url":
                    url = part.get("image_url", {}).get("url") if isinstance(part.get("image_url"), dict) else None
                    _expect(isinstance(url, str) and url.startswith(PNG_PREFIX),
                            f"messages[{index}]: pictures must be PNG data URLs")
                    try:
                        picture = base64.b64decode(url[len(PNG_PREFIX):], validate=True)
                    except ValueError:
                        picture = b""
                    _expect(picture.startswith(b"\x89PNG\r\n\x1a\n"), f"messages[{index}]: picture is not a PNG")
                else:
                    raise RequestError(f"messages[{index}]: unsupported content part {kind!r}")
        else:
            raise RequestError(f"messages[{index}]: unsupported role {role!r}")
    _expect(messages[-1].get("role") in ("user", "tool"), "The last message must come from the user or a tool")
    _expect(not pending, "The last assistant tool call has no tool message")


def newest_observation(messages: list[dict[str, Any]]) -> tuple[dict[str, Any], list[bytes]]:
    """The newest observation turn: its text fields and its pictures, head camera first."""
    for message in reversed(messages):
        if message.get("role") == "user" and isinstance(message.get("content"), list):
            parts = message["content"]
            pictures = [
                base64.b64decode(part["image_url"]["url"][len(PNG_PREFIX):])
                for part in parts if part.get("type") == "image_url"
            ]
            return json.loads(parts[0]["text"]), pictures
    raise RequestError("The prompt holds no observation")


class FakeOpenAI:
    """Request checks, scripted answers and a record of what was received."""

    def __init__(self, api_key: str, *, flawed_first_answer: bool = False) -> None:
        self.api_key = api_key
        self.flawed_first_answer = flawed_first_answer
        self.requests: list[dict[str, Any]] = []
        self.rejections: list[str] = []
        self._targets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def handle(self, path: str, authorization: str | None, raw: bytes) -> tuple[int, dict[str, Any]]:
        def error(status: int, message: str, kind: str) -> tuple[int, dict[str, Any]]:
            self.rejections.append(message)
            return status, {"error": {"message": message, "type": kind, "param": None, "code": None}}

        if path.rstrip("/") != "/v1/chat/completions":
            return error(404, f"Unknown path: {path}", "invalid_request_error")
        if authorization != f"Bearer {self.api_key}":
            return error(401, "Incorrect API key provided.", "invalid_request_error")
        try:
            body = json.loads(raw)
            check_request(body)
            observation, pictures = newest_observation(body["messages"])
            with self._lock:
                if observation["step_id"] == 0:
                    _expect(bool(pictures), "The first observation of an episode has no pictures")
                    self._targets = solver.locate(pictures[0])
                decision = solver.plan(observation, self._targets)
                if self.flawed_first_answer and not self.requests:
                    # Farther than the action contract allows; the agent must hand it back.
                    decision["target"]["left"]["position"][2] += 0.5
                self.requests.append(body)
                number = len(self.requests)
        except (ValueError, KeyError, TypeError, SystemExit) as failure:
            return error(400, str(failure), "invalid_request_error")
        return 200, {
            "id": f"chatcmpl-fake-{number}", "object": "chat.completion", "created": int(time.time()),
            "model": SERVED_MODEL, "system_fingerprint": "fp_fake",
            "choices": [{
                "index": 0, "finish_reason": "tool_calls", "logprobs": None,
                "message": {
                    "role": "assistant", "content": None, "refusal": None,
                    "tool_calls": [{
                        "id": f"call_fake_{number}", "type": "function",
                        "function": {"name": TOOL_NAME, "arguments": json.dumps(decision)},
                    }],
                },
            }],
            "usage": {
                "prompt_tokens": PROMPT_TOKENS, "completion_tokens": COMPLETION_TOKENS,
                "total_tokens": PROMPT_TOKENS + COMPLETION_TOKENS,
                "prompt_tokens_details": {"cached_tokens": 0, "audio_tokens": 0},
                "completion_tokens_details": {"reasoning_tokens": 0, "audio_tokens": 0},
            },
        }


def make_server(fake: FakeOpenAI, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            status, payload = fake.handle(self.path, self.headers.get("Authorization"), raw)
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: Any) -> None:
            pass

    return ThreadingHTTPServer((host, port), Handler)


def main(argv: list[str] | None = None) -> int:
    """Serve the fake endpoint on its own, for a Harbor run of the mock task."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--key-env", default="ROBODOJO_FAKE_OPENAI_KEY",
                        help="Environment variable holding the throwaway key that requests must carry")
    args = parser.parse_args(argv)
    key = os.environ.get(args.key_env)
    if not key:
        parser.error(f"Set {args.key_env} to any throwaway value; requests must carry it")
    server = make_server(FakeOpenAI(key), args.host, args.port)
    print(f"Fake OpenAI endpoint (not a model): http://{args.host}:{server.server_address[1]}/v1", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
