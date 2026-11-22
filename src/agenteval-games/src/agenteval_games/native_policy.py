"""Shared prospective parity policy; no tools, system prompt, or automatic retries.

This preserves AgentGym BaseTask/APIAgent's conversation protocol. API settings
are supplied explicitly, not represented as the settings of a historical run.
The host owns model credentials; only raw actions reach the game container.
"""

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


POLICY_VERSION = "0.1.0"
ROUND_LIMITS = {"maze": 15, "wordle": 8}
DEFAULT_EPISODE_TIMEOUT_SEC = 1500.0
ACKNOWLEDGEMENT = "OK. I'll follow your instructions and try my best to solve the task."


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Expected an HTTP(S) base URL without credentials, query, or fragment"
        )
    return value.rstrip("/")


# Request fields the policy owns; extra_body may not set or override them.
RESERVED_FIELDS = frozenset(
    {
        "model",
        "messages",
        "temperature",
        "max_tokens",
        "top_p",
        "stream",
        "n",
        "tools",
        "tool_choice",
    }
)


@dataclass(frozen=True)
class ModelSettings:
    """Request settings, identical on both sides.

    temperature, max_tokens or top_p set to None is left out of the request, for
    models that reject that field. extra_body adds provider-specific fields such
    as max_completion_tokens or reasoning_effort.
    """

    model: str
    temperature: float | None
    max_tokens: int | None
    request_timeout_sec: float
    top_p: float | None = 1.0
    extra_body: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("An exact model identifier is required")
        if self.max_tokens is not None and (
            type(self.max_tokens) is not int or self.max_tokens <= 0
        ):
            raise ValueError("max_tokens must be a positive integer")
        for name, value, low, high in (
            ("temperature", self.temperature, 0, 2),
            ("top_p", self.top_p, 0, 1),
            ("request_timeout_sec", self.request_timeout_sec, 0, float("inf")),
        ):
            if value is None and name != "request_timeout_sec":
                continue
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(name + " must be finite")
            if value < low or value > high or (name != "temperature" and value == 0):
                raise ValueError("Invalid " + name)
        if not isinstance(self.extra_body, dict) or not all(
            isinstance(key, str) for key in self.extra_body
        ):
            raise ValueError("extra_body must be a JSON object")
        reserved = RESERVED_FIELDS.intersection(self.extra_body)
        if reserved:
            raise ValueError("extra_body cannot set " + ", ".join(sorted(reserved)))
        try:
            json.dumps(self.extra_body, allow_nan=False)
        except (TypeError, ValueError):
            raise ValueError("extra_body must be JSON-serializable") from None

    def request_body(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        body: dict[str, Any] = {"model": self.model, "messages": messages}
        for name in ("temperature", "max_tokens", "top_p"):
            value = getattr(self, name)
            if value is not None:
                body[name] = value
        body.update(self.extra_body)
        return body


class ProtocolError(RuntimeError):
    """An infrastructure/protocol error, never a benchmark failure score."""


def request_json(
    url: str, *, payload: Any = None, timeout: float, api_key: str | None = None
) -> Any:
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if api_key is not None:
        headers["Authorization"] = "Bearer " + api_key
    request = Request(url, data=data, headers=headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as exc:
        # Do not log error bodies: providers may echo request credentials/content.
        raise ProtocolError(
            "HTTP request failed with status " + str(exc.code)
        ) from None
    except (URLError, TimeoutError, OSError) as exc:
        raise ProtocolError("HTTP request failed: " + type(exc).__name__) from None
    except (ValueError, UnicodeDecodeError):
        raise ProtocolError("HTTP endpoint returned invalid JSON") from None


class ChatCompletionsClient:
    def __init__(self, settings: ModelSettings, base_url: str, api_key: str):
        if not api_key:
            raise ValueError("A model API key is required")
        self.settings = settings
        self.base_url = validate_base_url(base_url)
        self._api_key = api_key

    async def complete(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        result = await asyncio.to_thread(
            request_json,
            self.base_url + "/chat/completions",
            payload=self.settings.request_body(messages),
            timeout=self.settings.request_timeout_sec,
            api_key=self._api_key,
        )
        try:
            choice = result["choices"][0]
            content = choice["message"]["content"]
            if not isinstance(content, str) or choice["message"].get("tool_calls"):
                raise ValueError("Expected a text response")
            usage = result.get("usage") or {}
            counts = {
                "input_tokens": usage.get("prompt_tokens", 0) or 0,
                "output_tokens": usage.get("completion_tokens", 0) or 0,
                "cache_tokens": (usage.get("prompt_tokens_details") or {}).get(
                    "cached_tokens", 0
                )
                or 0,
            }
            if any(type(value) is not int or value < 0 for value in counts.values()):
                raise ValueError("Invalid token counts")
            return {
                "content": content,
                "finish_reason": choice.get("finish_reason"),
                "usage": counts,
            }
        except (KeyError, IndexError, TypeError, ValueError, AttributeError):
            raise ProtocolError(
                "Model endpoint returned an invalid text completion"
            ) from None


def initial_messages(game: str, observation: str) -> list[dict[str, str]]:
    if game not in ROUND_LIMITS:
        raise ValueError("Unknown game: " + game)
    role = "maze solver" if game == "maze" else "wordle player"
    return [
        {"role": "user", "content": "You are an expert " + role + "."},
        {"role": "assistant", "content": ACKNOWLEDGEMENT},
        {"role": "user", "content": observation},
    ]


def validate_state(state: Any) -> dict[str, Any]:
    if not isinstance(state, dict) or "error" in state:
        raise ProtocolError("Game endpoint returned an error or invalid object")
    if (
        not isinstance(state.get("observation"), str)
        or type(state.get("done")) is not bool
        or type(state.get("reward")) not in (int, float)
        or not math.isfinite(state["reward"])
    ):
        raise ProtocolError("Game endpoint returned invalid observation/reward/done")
    return state


def new_report(settings: ModelSettings, side: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "policy_version": POLICY_VERSION,
        "side": side,
        "settings": asdict(settings),
        "started_at": timestamp(),
        "status": "running",
        "attempts": [],
        "messages": [],
        "usage": {"input_tokens": 0, "output_tokens": 0, "cache_tokens": 0},
    }


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


async def _run_episode(
    *,
    game: str,
    initial: dict[str, Any],
    complete: Callable[[list[dict[str, str]]], Awaitable[dict[str, Any]]],
    step: Callable[[str], Awaitable[dict[str, Any]]],
    report: dict[str, Any],
    checkpoint: Callable[[], None] = lambda: None,
) -> dict[str, Any]:
    """Same conversation and turn loop on both original and Harbor sides.

    The caller checkpoints startup/reset failures. This function checkpoints all
    model/step attempts and re-raises errors or cancellation. An uncertain POST
    is never retried: retrying could silently consume two game turns.
    """
    try:
        initial = validate_state(initial)
        if initial["done"] or initial.get("episode_finished", False):
            raise ProtocolError("Expected a fresh episode")
        limit = ROUND_LIMITS[game]
        if initial.get("turns_remaining", limit) != limit:
            raise ProtocolError("Episode turn budget does not match AgentEval")
        messages = initial_messages(game, initial["observation"])
        report.update(game=game, max_rounds=limit, initial=initial, messages=messages)
        checkpoint()
        result = initial
        for turn in range(limit):
            attempt = {
                "turn": turn + 1,
                "status": "request_started",
                "started_at": timestamp(),
            }
            report["attempts"].append(attempt)
            checkpoint()
            completion = await complete([dict(message) for message in messages])
            action = completion["content"]
            if not isinstance(action, str):
                raise ProtocolError("Model response must be text")
            attempt.update(
                status="model_received",
                action=action,
                finish_reason=completion.get("finish_reason"),
                usage=completion.get("usage", {}),
            )
            for name in report["usage"]:
                report["usage"][name] += completion.get("usage", {}).get(name, 0)
            messages.append({"role": "assistant", "content": action})
            checkpoint()
            result = validate_state(await step(action))
            messages.append({"role": "user", "content": result["observation"]})
            attempt.update(status="step_received", result=result, ended_at=timestamp())
            report["last_reward"] = result["reward"]
            checkpoint()
            if result["done"]:
                break
            if result.get("episode_finished", False) and turn + 1 < limit:
                raise ProtocolError("Game truncated before the upstream harness limit")
        report.update(
            status="completed",
            ended_at=timestamp(),
            reward=result["reward"],
            done=result["done"],
            truncated=not result["done"],
            outcome=(
                "success"
                if result["reward"] == 1
                else "failure"
                if result["done"]
                else "truncated"
            ),
        )
        checkpoint()
        return report
    except (asyncio.CancelledError, KeyboardInterrupt):
        report.update(status="interrupted", ended_at=timestamp())
        checkpoint()
        raise
    except Exception as exc:
        report.update(
            status="error", error_type=type(exc).__name__, ended_at=timestamp()
        )
        checkpoint()
        raise


async def run_episode(
    *,
    game: str,
    initial: dict[str, Any],
    complete: Callable[[list[dict[str, str]]], Awaitable[dict[str, Any]]],
    step: Callable[[str], Awaitable[dict[str, Any]]],
    report: dict[str, Any],
    checkpoint: Callable[[], None] = lambda: None,
    episode_timeout_sec: float = DEFAULT_EPISODE_TIMEOUT_SEC,
) -> dict[str, Any]:
    """Apply the same explicit wall-clock deadline to both prospective runs.

    Unlike the historical upstream APIAgent's unbounded retry loop, failed or
    timed-out episodes are infrastructure errors and cannot produce a score.
    Harbor's outer task timeout must exceed this deadline plus transport/setup.
    """
    if not math.isfinite(episode_timeout_sec) or episode_timeout_sec <= 0:
        raise ValueError("episode_timeout_sec must be positive and finite")
    report["episode_timeout_sec"] = episode_timeout_sec
    try:
        async with asyncio.timeout(episode_timeout_sec):
            return await _run_episode(
                game=game,
                initial=initial,
                complete=complete,
                step=step,
                report=report,
                checkpoint=checkpoint,
            )
    except TimeoutError:
        report.update(
            status="error",
            error_type="TimeoutError",
            timeout_scope="episode",
            ended_at=timestamp(),
        )
        checkpoint()
        raise
