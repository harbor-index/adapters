"""Harbor parity agent for HAEnv M2 diagnosis tasks.

Mirrors upstream `haenv.interaction.query.gated.solve_gated_round`:
- Fetches upstream QUERY_SYSTEM, rendered question, and OpenAI tool definitions
  from the isolated `ehr-server` sidecar (`GET /specs`).
- Runs an OpenAI-compatible function-calling loop (up to max_rounds=32,
  max_calls=256), dispatching each tool call (and batching multiple `order_test`
  calls in one assistant turn into a single purchase round) to `POST /tool`.
- Writes the model's final answer text to `/workspace/answer.json`.

Usage (from adapters repo root):
  PYTHONPATH="$PWD/src/haenv/src" harbor run \\
    -p datasets/haenv \\
    -a haenv.parity_agent:HAEnvParityAgent \\
    -m openai/gpt-5-mini
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shlex
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext


class HAEnvParityAgent(BaseAgent):
    """Thin Harbor agent matching upstream HAEnv's query + budgeted workup loop."""

    def __init__(
        self,
        *args: Any,
        max_rounds: int = 32,
        max_calls: int = 256,
        temperature: float | None = 0.0,
        max_tokens: int = 16384,
        reasoning_effort: str | None = None,
        request_timeout_sec: int = 300,
        api_base: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._max_rounds = int(max_rounds)
        self._max_calls = int(max_calls)
        self._temperature = temperature
        self._max_tokens = int(max_tokens)
        self._reasoning_effort = reasoning_effort
        self._request_timeout_sec = int(request_timeout_sec)
        self._api_base = api_base

    @staticmethod
    def name() -> str:
        return "haenv-parity-agent"

    def version(self) -> str | None:
        return "1.0.0"

    async def setup(self, environment: BaseEnvironment) -> None:
        return

    def _resolve_endpoint(self) -> tuple[str, str, str]:
        if not self.model_name:
            raise RuntimeError("model_name is required for HAEnvParityAgent")

        model = self.model_name
        base_url = (
            self._api_base
            or getattr(self, "base_url", None)
            or getattr(self, "_base_url", None)
            or os.environ.get("HAENV_PARITY_BASE_URL")
            or ""
        ).strip()

        if os.environ.get("OPENROUTER_API_KEY") and (
            not base_url or "openrouter.ai" in base_url
        ):
            api_key = os.environ["OPENROUTER_API_KEY"]
            url = (base_url or "https://openrouter.ai/api/v1/chat/completions").rstrip("/")
            if not url.endswith("/chat/completions"):
                url += "/chat/completions"
            return url, api_key, model

        if os.environ.get("OPENAI_API_KEY"):
            api_key = os.environ["OPENAI_API_KEY"]
            url = (base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
            if not url.endswith("/chat/completions"):
                url += "/chat/completions"
            if model.startswith("openai/"):
                model = model.removeprefix("openai/")
            return url, api_key, model

        if os.environ.get("GOOGLE_API_KEY"):
            api_key = os.environ["GOOGLE_API_KEY"]
            url = (
                base_url
                or "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
            ).rstrip("/")
            if not url.endswith("/chat/completions"):
                url += "/chat/completions"
            if model.startswith("google/") or model.startswith("gemini/"):
                model = model.split("/", 1)[1]
            return url, api_key, model

        raise RuntimeError(
            "HAEnvParityAgent requires OPENROUTER_API_KEY, OPENAI_API_KEY, or GOOGLE_API_KEY"
        )

    def _chat(
        self,
        url: str,
        api_key: str,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        tool_choice: str,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": self._max_tokens,
        }
        if self._temperature is not None:
            body["temperature"] = self._temperature
        if self._reasoning_effort and "openrouter.ai" in url:
            body["reasoning"] = {"effort": self._reasoning_effort}
        if tools and tool_choice != "none":
            body["tools"] = [{"type": "function", "function": t} for t in tools]
            body["tool_choice"] = tool_choice

        payload_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
        last_exc: Exception | None = None
        for attempt in range(1, 5):
            req = urllib.request.Request(
                url,
                data=payload_bytes,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self._request_timeout_sec) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, dict) and data.get("error") and not data.get("choices"):
                    raise RuntimeError(f"API error body: {str(data['error'])[:300]}")
                return data
            except urllib.error.HTTPError as exc:
                err_body = exc.read().decode("utf-8", errors="replace")[:300]
                last_exc = RuntimeError(f"Chat HTTP {exc.code}: {err_body}")
                if exc.code not in (408, 429, 500, 502, 503, 504) or attempt == 4:
                    raise last_exc from exc
                time.sleep(min(2.0 * attempt, 10.0))
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt == 4:
                    raise
                time.sleep(min(2.0 * attempt, 10.0))
        raise last_exc or RuntimeError("Chat request failed")

    async def _ehr_get(self, environment: BaseEnvironment, path: str) -> dict[str, Any]:
        cmd = (
            "python3 -c "
            + shlex.quote(
                "import os, urllib.request; "
                f"u=os.environ.get('HAENV_EHR_URL','http://ehr-server:8000').rstrip('/')+{path!r}; "
                "print(urllib.request.urlopen(u, timeout=60).read().decode('utf-8'))"
            )
        )
        res = await environment.exec(cmd)
        if res.return_code != 0:
            raise RuntimeError(f"EHR GET {path} failed: {res.stderr or res.stdout}")
        return json.loads(res.stdout)

    async def _ehr_post_tool(
        self, environment: BaseEnvironment, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        payload_b64 = base64.b64encode(
            json.dumps({"name": name, "arguments": arguments}, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        script = (
            "import base64, os, urllib.request; "
            f"data=base64.b64decode({payload_b64!r}); "
            "u=os.environ.get('HAENV_EHR_URL','http://ehr-server:8000').rstrip('/')+'/tool'; "
            "req=urllib.request.Request(u, data=data, headers={'Content-Type':'application/json'}, method='POST'); "
            "print(urllib.request.urlopen(req, timeout=60).read().decode('utf-8'))"
        )
        res = await environment.exec("python3 -c " + shlex.quote(script))
        if res.return_code != 0:
            return {"error": f"Tool transport failed: {res.stderr or res.stdout}"}
        return json.loads(res.stdout)

    async def _write_answer(self, environment: BaseEnvironment, answer_text: str) -> None:
        b64 = base64.b64encode(answer_text.encode("utf-8")).decode("ascii")
        script = (
            "import base64, pathlib; "
            f"pathlib.Path('/workspace/answer.json').write_bytes(base64.b64decode({b64!r}))"
        )
        res = await environment.exec("python3 -c " + shlex.quote(script))
        if res.return_code != 0:
            raise RuntimeError(f"Failed writing /workspace/answer.json: {res.stderr or res.stdout}")

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        url, api_key, model = self._resolve_endpoint()
        spec_doc = await self._ehr_get(environment, "/specs")
        max_rounds = min(self._max_rounds, int(spec_doc.get("max_rounds") or 32))
        max_calls = min(self._max_calls, int(spec_doc.get("max_calls") or 256))
        force_final = str(spec_doc.get("force_final") or "Return your final JSON answer now without tool calls.")

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": str(spec_doc["system"])},
            {"role": "user", "content": str(spec_doc["question"])},
        ]

        n_calls = 0
        final_answer = ""
        total_input_tokens = 0
        total_output_tokens = 0

        for round_idx in range(max_rounds + 1):
            is_final = round_idx >= max_rounds or n_calls >= max_calls
            if is_final:
                messages.append({"role": "user", "content": force_final})
                tools: list[dict[str, Any]] = []
            else:
                latest_specs = await self._ehr_get(environment, "/specs")
                tools = list(latest_specs.get("tools") or [])

            resp = await asyncio.to_thread(
                self._chat,
                url,
                api_key,
                model,
                messages,
                tools,
                "none" if is_final else "auto",
            )
            usage = resp.get("usage") or {}
            total_input_tokens += int(usage.get("prompt_tokens") or 0)
            total_output_tokens += int(usage.get("completion_tokens") or 0)

            choice = (resp.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            content = msg.get("content") or ""
            tool_calls = list(msg.get("tool_calls") or [])

            assistant_entry: dict[str, Any] = {"role": "assistant", "content": content}
            if tool_calls:
                assistant_entry["tool_calls"] = tool_calls
            messages.append(assistant_entry)

            if is_final:
                final_answer = "" if tool_calls else content
                break

            if not tool_calls:
                final_answer = content
                break

            # Mirror upstream solve_gated_round: batch multiple order_test calls in one turn
            # into a single purchase round so purchase_round increments once per turn.
            non_orders: list[tuple[str, str, dict[str, Any]]] = []
            orders: list[tuple[str, str]] = []
            for idx, tc in enumerate(tool_calls):
                cid = str(tc.get("id") or f"call_{round_idx}_{idx}")
                fn = tc.get("function") or {}
                fname = str(fn.get("name") or "")
                raw_args = fn.get("arguments")
                if isinstance(raw_args, str):
                    try:
                        parsed_args = json.loads(raw_args) if raw_args.strip() else {}
                    except ValueError:
                        parsed_args = {}
                elif isinstance(raw_args, dict):
                    parsed_args = raw_args
                else:
                    parsed_args = {}

                if fname == "order_test" and isinstance(parsed_args.get("target"), str):
                    orders.append((cid, parsed_args["target"]))
                else:
                    non_orders.append((cid, fname, parsed_args))

            for cid, fname, parsed_args in non_orders:
                n_calls += 1
                result = await self._ehr_post_tool(environment, fname, parsed_args)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": cid,
                        "name": fname,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )

            if orders:
                n_calls += len(orders)
                if len(orders) == 1:
                    cid, target = orders[0]
                    res = await self._ehr_post_tool(environment, "order_test", {"target": target})
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": cid,
                            "name": "order_test",
                            "content": json.dumps(res, ensure_ascii=False),
                        }
                    )
                else:
                    batch = await self._ehr_post_tool(
                        environment,
                        "order_test",
                        {"targets": [t for _, t in orders]},
                    )
                    items = batch.get("results") or []
                    for i, (cid, _target) in enumerate(orders):
                        item_res = items[i] if i < len(items) else {"error": "Missing purchase result"}
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": cid,
                                "name": "order_test",
                                "content": json.dumps(item_res, ensure_ascii=False),
                            }
                        )

        await self._write_answer(environment, final_answer)

        context.n_input_tokens = total_input_tokens
        context.n_output_tokens = total_output_tokens
        (self.logs_dir / "transcript.json").write_text(
            json.dumps(messages, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
