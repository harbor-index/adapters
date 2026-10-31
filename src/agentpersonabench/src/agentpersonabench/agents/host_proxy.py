"""Host proxy the in-container web agent calls.

Matches APB's ``llm_proxy`` ``POST /chat`` shape. The container never sees the
provider key. This forwarder speaks the OpenAI-compatible chat API that
DashScope and the other APB OpenAI-compatible arms use.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _api_model(model_name: str) -> str:
    name = (model_name or "").strip()
    if "/" in name:
        return name.split("/", 1)[1]
    return name


def _is_claude(model_name: str) -> bool:
    return "claude" in (model_name or "").lower()


class HostChatProxy:
    def __init__(self, model_name: str) -> None:
        self.model_name = _api_model(model_name)
        self._anthropic = _is_claude(self.model_name)
        if self._anthropic:
            key = (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY") or "").strip()
            if not key:
                raise RuntimeError("ANTHROPIC_API_KEY is unset")
            self._url = "https://api.anthropic.com/v1/messages"
            self._key = key
        else:
            key = os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("LLM_API_KEY") or ""
            if not key:
                raise RuntimeError("DASHSCOPE_API_KEY is unset")
            base = (
                os.environ.get("DASHSCOPE_API_BASE")
                or os.environ.get("LLM_BASE_URL")
                or "https://dashscope.aliyuncs.com/compatible-mode/v1"
            ).rstrip("/")
            self._url = f"{base}/chat/completions"
            self._key = key
        self.calls: list[dict] = []
        self._server = ThreadingHTTPServer(("0.0.0.0", 0), self._handler())
        self.host, self.port = self._server.server_address
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def container_url(self) -> str:
        return f"http://host.docker.internal:{self.port}"

    def start(self) -> "HostChatProxy":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _handler(self):
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args) -> None:
                return

            def _send(self, code: int, payload: dict) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if self.path == "/health":
                    self._send(200, {"ok": True, "model": proxy.model_name})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self) -> None:
                if self.path != "/chat":
                    self._send(404, {"error": "not found"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    req = json.loads(self.rfile.read(length) or b"{}")
                    text, usage = proxy.complete(req)
                    self._send(200, {"text": text, "usage": usage})
                except Exception as exc:
                    self._send(500, {"error": str(exc)})

        return Handler

    def complete(self, req: dict) -> tuple[str, dict]:
        messages = _request_messages(req)
        model = _api_model(req.get("model") or self.model_name)
        if _is_claude(model):
            return self._complete_anthropic(messages, model, int(req.get("max_tokens", 1024)))
        return self._complete_openai(messages, model, req)

    def _complete_anthropic(self, messages: list, model: str, max_tokens: int) -> tuple[str, dict]:
        from agentpersonabench.judge_client import _claude_thinking, _split_system

        system, convo = _split_system(messages)
        thinking, max_tokens = _claude_thinking(model, max_tokens)
        payload: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": item["role"], "content": _anthropic_content(item.get("content", ""))}
                for item in convo
            ],
            **thinking,
        }
        if system:
            payload["system"] = system
        data = self._post(
            payload,
            {
                "Content-Type": "application/json",
                "x-api-key": self._key,
                "anthropic-version": "2023-06-01",
            },
        )
        text = "".join(
            block.get("text", "")
            for block in data.get("content") or []
            if isinstance(block, dict) and block.get("type") == "text"
        )
        raw = data.get("usage") or {}
        usage = {
            "prompt_tokens": int(raw.get("input_tokens") or 0),
            "completion_tokens": int(raw.get("output_tokens") or 0),
        }
        self.calls.append({"model": model, **usage})
        return text, usage

    def _complete_openai(self, messages: list, model: str, req: dict) -> tuple[str, dict]:
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": int(req.get("max_tokens", 1024)),
            "temperature": float(req.get("temperature", 0.0)),
        }
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self._url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._key}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"LLM HTTP {exc.code}: {detail}") from exc
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        usage = data.get("usage") or {}
        self.calls.append({"model": payload["model"], **usage})
        return text, usage

    def _post(self, payload: dict, headers: dict) -> dict:
        request = urllib.request.Request(
            self._url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"LLM HTTP {exc.code}: {detail}") from exc


def _request_messages(req: dict) -> list:
    messages = req.get("messages")
    if messages:
        return messages
    messages = []
    if req.get("system"):
        messages.append({"role": "system", "content": req["system"]})
    user = req.get("user", "")
    images = req.get("images") or []
    if images:
        media = req.get("image_media_type", "image/png")
        user = [{"type": "text", "text": user}] + [
            {"type": "image_url", "image_url": {"url": f"data:{media};base64,{b64}"}}
            for b64 in images
        ]
    messages.append({"role": "user", "content": user})
    return messages


def _anthropic_content(content):
    if isinstance(content, str):
        return content
    blocks = []
    for part in content:
        if not isinstance(part, dict):
            blocks.append({"type": "text", "text": str(part)})
            continue
        if part.get("type") == "image_url":
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            head, _, data = str(url).partition(",")
            if not head.startswith("data:") or not head.endswith(";base64"):
                raise ValueError("image parts must be base64 data: URLs")
            media = head[len("data:") : -len(";base64")]
            blocks.append(
                {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}
            )
        else:
            blocks.append({"type": "text", "text": part.get("text", "")})
    return blocks
