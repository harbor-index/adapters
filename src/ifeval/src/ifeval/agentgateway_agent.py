from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, override
from urllib.parse import urlsplit, urlunsplit

import httpx
import harbor.agents.base as _harbor_base
from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

_RESPONSE_PATH = "/workspace/response.txt"
_HTTP_TIMEOUT = httpx.Timeout(10.0, read=None)
_BASE_URL_ALIASES = (
    "AGENTGATEWAY_BASE_URL",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
)
_API_KEY_ALIASES = ("AGENTGATEWAY_API_KEY", "OPENAI_API_KEY")
_AgentCapabilities = getattr(_harbor_base, "AgentCapabilities", None)


class AgentGatewayAgent(BaseAgent):
    if _AgentCapabilities is not None:
        capabilities = _AgentCapabilities.model_validate(
            {
                key: True
                for key in ("base_url", "reasoning_effort")
                if key in _AgentCapabilities.model_fields
            }
        )

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        api_base: str | None = None,
        reasoning_effort: str | None = None,
        max_tokens: int | None = None,
        extra_env: dict[str, str] | None = None,
        wire_api: str = "responses",
        **kwargs: Any,
    ) -> None:
        if not model_name:
            raise ValueError("AgentGateway requires a model_name")
        if wire_api not in ("responses", "messages", "completions"):
            raise ValueError(
                "AgentGateway wire_api must be responses, messages, or completions"
            )
        if max_tokens is not None and (
            isinstance(max_tokens, bool)
            or not isinstance(max_tokens, int)
            or max_tokens <= 0
        ):
            raise ValueError("AgentGateway max_tokens must be a positive integer")
        if wire_api == "messages" and max_tokens is None:
            raise ValueError("AgentGateway messages requires explicit max_tokens")

        super().__init__(
            logs_dir=logs_dir,
            model_name=model_name,
            extra_env=extra_env,
            **kwargs,
        )
        self._api_base = api_base
        self._reasoning_effort = reasoning_effort
        self._max_tokens = max_tokens
        self._wire_api = wire_api

    @staticmethod
    @override
    def name() -> str:
        return "agentgateway"

    @override
    def version(self) -> str | None:
        return None

    @override
    async def setup(self, environment: BaseEnvironment) -> None:
        pass

    @override
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        api_base = (
            self._api_base
            if self._api_base is not None
            else self._get_env(*_BASE_URL_ALIASES)
        )
        if not api_base:
            raise ValueError("AgentGateway requires an API base URL")

        payload = self._build_request(instruction)

        headers: dict[str, str] = {}
        api_key = self._get_env(*_API_KEY_ALIASES)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        if self._wire_api == "messages":
            headers["anthropic-version"] = "2023-06-01"

        suffix = (
            "chat/completions" if self._wire_api == "completions" else self._wire_api
        )
        endpoint = api_base.rstrip("/") + "/" + suffix
        start_time = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                response = await client.post(
                    endpoint,
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"AgentGateway returned HTTP {exc.response.status_code}"
            ) from None
        except httpx.RequestError as exc:
            raise RuntimeError(
                f"AgentGateway request failed: {type(exc).__name__}"
            ) from None
        latency_sec = time.monotonic() - start_time

        try:
            response_body = response.json()
        except ValueError:
            raise RuntimeError("AgentGateway returned a non-JSON response") from None
        if not isinstance(response_body, dict):
            raise RuntimeError("AgentGateway response must be a JSON object")
        if self._wire_api == "messages":
            output_text, status, incomplete_reason = self._parse_messages(response_body)
            stop_reason = response_body["stop_reason"]
        elif self._wire_api == "completions":
            output_text, status, incomplete_reason = self._parse_completions(
                response_body
            )
            stop_reason = response_body["choices"][0]["finish_reason"]
        else:
            output_text, status, incomplete_reason = self._parse_response(response_body)
            details = response_body.get("incomplete_details")
            stop_reason = details.get("reason") if isinstance(details, dict) else None
            if stop_reason not in ("max_tokens", "max_output_tokens"):
                stop_reason = None
        usage = self._populate_context(context, response_body)

        self.logs_dir.mkdir(parents=True, exist_ok=True)
        response_path = self.logs_dir / "response.txt"
        response_path.write_text(output_text, encoding="utf-8")
        await environment.upload_file(
            source_path=response_path,
            target_path=_RESPONSE_PATH,
        )
        self._write_metadata(
            api_base=api_base,
            endpoint=endpoint,
            request_payload=payload,
            response_payload=response_body,
            status=status,
            incomplete_reason=incomplete_reason,
            stop_reason=stop_reason,
            usage=usage,
            latency_sec=latency_sec,
        )
        self.logger.debug(
            "AgentGateway request completed in %.3f seconds",
            latency_sec,
        )

    def _build_request(self, instruction: str) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": self.model_name}
        if self._wire_api == "responses":
            payload.update(input=instruction, store=False)
            if self._reasoning_effort is not None:
                payload["reasoning"] = {"effort": self._reasoning_effort}
            if self._max_tokens is not None:
                payload["max_output_tokens"] = self._max_tokens
        else:
            payload["messages"] = [{"role": "user", "content": instruction}]
            if self._max_tokens is not None:
                payload["max_tokens"] = self._max_tokens
            if self._reasoning_effort is not None:
                if self._wire_api == "messages":
                    payload["thinking"] = {"type": "adaptive"}
                    payload["output_config"] = {"effort": self._reasoning_effort}
                else:
                    payload["reasoning_effort"] = self._reasoning_effort
        return payload

    @staticmethod
    def _redact_url(url: str) -> str:
        parsed = urlsplit(url)
        if not parsed.scheme or not parsed.netloc:
            return "<redacted>"
        netloc = parsed.netloc.rsplit("@", maxsplit=1)[-1]
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))

    @staticmethod
    def _parse_response(response: Any) -> tuple[str, str, str | None]:
        if not isinstance(response, dict):
            raise RuntimeError("AgentGateway response must be a JSON object")

        status = response.get("status")
        if status not in (
            "completed",
            "incomplete",
            "failed",
            "cancelled",
            "queued",
            "in_progress",
        ):
            raise RuntimeError("AgentGateway response has unsupported status")
        incomplete_details = response.get("incomplete_details")
        incomplete_reason = None
        if isinstance(incomplete_details, dict):
            reason = incomplete_details.get("reason")
            if reason in ("max_output_tokens", "max_tokens"):
                incomplete_reason = reason

        if status != "completed" and not (
            status == "incomplete"
            and incomplete_reason in ("max_output_tokens", "max_tokens")
        ):
            raise RuntimeError(f"AgentGateway response has status {status}")
        if status == "incomplete":
            incomplete_reason = "max_output_tokens"

        output = response.get("output")
        if not isinstance(output, list):
            raise RuntimeError("AgentGateway response output must be a list")

        text_chunks: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                raise RuntimeError(
                    "AgentGateway response output item must be an object"
                )
            if item.get("type") != "message":
                continue

            content = item.get("content")
            if not isinstance(content, list):
                raise RuntimeError("AgentGateway message content must be a list")
            for content_item in content:
                if not isinstance(content_item, dict):
                    raise RuntimeError(
                        "AgentGateway message content item must be an object"
                    )
                if content_item.get("type") != "output_text":
                    continue

                text = content_item.get("text")
                if not isinstance(text, str):
                    raise RuntimeError("AgentGateway output_text text must be a string")
                text_chunks.append(text)

        return "".join(text_chunks), status, incomplete_reason

    @staticmethod
    def _parse_messages(response: dict[str, Any]) -> tuple[str, str, str | None]:
        if response.get("type") != "message" or response.get("role") != "assistant":
            raise RuntimeError(
                "AgentGateway Messages response must be an assistant message"
            )
        stop_reason = response.get("stop_reason")
        if stop_reason not in ("end_turn", "stop_sequence", "refusal", "max_tokens"):
            raise RuntimeError(
                "AgentGateway Messages response has unsupported stop_reason"
            )
        content = response.get("content")
        if not isinstance(content, list):
            raise RuntimeError("AgentGateway Messages content must be a list")
        chunks: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                raise RuntimeError(
                    "AgentGateway Messages content block must be an object"
                )
            if block.get("type") in ("thinking", "redacted_thinking"):
                continue
            if block.get("type") != "text" or not isinstance(block.get("text"), str):
                raise RuntimeError(
                    "AgentGateway Messages content must contain text blocks only"
                )
            chunks.append(block["text"])
        if not chunks:
            raise RuntimeError("AgentGateway Messages response contains no text")
        if stop_reason == "max_tokens":
            return "".join(chunks), "incomplete", "max_output_tokens"
        return "".join(chunks), "completed", None

    @staticmethod
    def _parse_completions(response: dict[str, Any]) -> tuple[str, str, str | None]:
        choices = response.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise RuntimeError(
                "AgentGateway Completions response must have exactly one choice"
            )
        choice = choices[0]
        if not isinstance(choice, dict):
            raise RuntimeError("AgentGateway Completions choice must be an object")
        finish_reason = choice.get("finish_reason")
        if finish_reason not in ("stop", "length"):
            raise RuntimeError(
                "AgentGateway Completions response has unsupported finish_reason"
            )
        message = choice.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise RuntimeError(
                "AgentGateway Completions choice must contain an assistant message"
            )
        if (
            message.get("tool_calls") not in (None, [])
            or message.get("function_call") is not None
        ):
            raise RuntimeError("AgentGateway Completions response contains tool calls")
        text = message.get("content")
        refusal = message.get("refusal")
        if refusal is not None and not isinstance(refusal, str):
            raise RuntimeError("AgentGateway Completions refusal must be a string")
        if text is None:
            text = refusal
        if not isinstance(text, str):
            raise RuntimeError("AgentGateway Completions response contains no text")
        if finish_reason == "length":
            return text, "incomplete", "max_output_tokens"
        return text, "completed", None

    def _populate_context(
        self,
        context: AgentContext,
        response: dict[str, Any],
    ) -> dict[str, Any] | None:
        usage = response.get("usage")
        if usage is None:
            return None

        if not isinstance(usage, dict):
            raise RuntimeError("AgentGateway usage must be an object")

        if self._wire_api == "messages":
            validated = self._validate_usage(
                usage,
                (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                ),
                {
                    "cache_creation": (
                        "ephemeral_5m_input_tokens",
                        "ephemeral_1h_input_tokens",
                    )
                },
            )
            input_tokens = validated.get("input_tokens")
            cache_read = validated.get("cache_read_input_tokens", 0)
            cache_creation = validated.get("cache_creation_input_tokens", 0)
            context.n_input_tokens = (
                input_tokens + cache_read + cache_creation
                if input_tokens is not None
                else None
            )
            context.n_cache_tokens = cache_read
            context.n_output_tokens = validated.get("output_tokens")
            return validated

        if self._wire_api == "completions":
            input_key, output_key = "prompt_tokens", "completion_tokens"
            counters = (
                input_key,
                output_key,
                "total_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            )
            details = {
                "prompt_tokens_details": (
                    "cached_tokens",
                    "audio_tokens",
                    "cache_write_tokens",
                ),
                "completion_tokens_details": (
                    "reasoning_tokens",
                    "audio_tokens",
                    "accepted_prediction_tokens",
                    "rejected_prediction_tokens",
                ),
            }
        else:
            input_key, output_key = "input_tokens", "output_tokens"
            counters = (input_key, output_key, "total_tokens")
            details = {
                "input_tokens_details": ("cached_tokens", "cache_write_tokens"),
                "output_tokens_details": ("reasoning_tokens",),
            }
        validated = self._validate_usage(
            usage,
            counters,
            details,
        )
        input_details = validated.get(f"{input_key}_details") or {}
        context.n_input_tokens = validated.get(input_key)
        context.n_output_tokens = validated.get(output_key)
        context.n_cache_tokens = input_details.get("cached_tokens", 0)
        return validated

    def _validate_usage(
        self,
        usage: dict[str, Any],
        counters: tuple[str, ...],
        details: dict[str, tuple[str, ...]],
        *,
        allow_null: bool = False,
    ) -> dict[str, Any]:
        validated: dict[str, Any] = {}
        for key in counters:
            if key in usage:
                value = self._usage_int(usage[key], key)
                if value is None and self._wire_api != "responses" and not allow_null:
                    raise RuntimeError(
                        f"AgentGateway usage {key} must be a nonnegative integer"
                    )
                validated[key] = value
        for key, fields in details.items():
            if key not in usage:
                continue
            value = usage[key]
            if value is None and self._wire_api == "responses":
                validated[key] = None
                continue
            if not isinstance(value, dict):
                raise RuntimeError(f"AgentGateway usage {key} must be an object")
            # The gateway serializes absent optional detail counters as null.
            validated[key] = self._validate_usage(value, fields, {}, allow_null=True)
        return validated

    @staticmethod
    def _usage_int(value: Any, key: str) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError(
                f"AgentGateway usage {key} must be a nonnegative integer"
            )
        return value

    def _write_metadata(
        self,
        *,
        api_base: str,
        endpoint: str,
        request_payload: dict[str, Any],
        response_payload: dict[str, Any],
        status: str,
        incomplete_reason: str | None,
        stop_reason: str | None,
        usage: dict[str, Any] | None,
        latency_sec: float,
    ) -> None:
        metadata = {
            "model_name": self.model_name,
            "response_model_name": response_payload.get("model"),
            "response_id": response_payload.get("id"),
            "api_base": self._redact_url(api_base),
            "endpoint": self._redact_url(endpoint),
            "request": {
                **request_payload,
                "input" if self._wire_api == "responses" else "messages": "<redacted>",
            },
            "response_path": _RESPONSE_PATH,
            "status": status,
            "incomplete_reason": incomplete_reason,
            "wire_api": self._wire_api,
            "stop_reason": stop_reason,
            "usage": usage,
            "input_token_accounting": (
                "input_tokens + cache_read_input_tokens + cache_creation_input_tokens"
                if self._wire_api == "messages"
                else "provider-reported (cache inclusion depends on gateway route)"
            ),
            "latency_sec": latency_sec,
        }
        (self.logs_dir / "agentgateway.json").write_text(
            json.dumps(metadata, indent=2),
            encoding="utf-8",
        )
