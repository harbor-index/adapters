"""The Direct-mode player: a vision model chooses bounded robot actions.

For every observation the player sends the native instruction, the robot state
and the camera frames to a vision model, which answers with one bounded
end-effector decision through a single tool. The decision is forwarded to the
broker in the simulator, which validates and executes it.

This module holds the whole decision loop and needs neither Harbor nor Docker.
The Harbor agent (``robodojo.direct_agent.DirectAgent``) and the original-side
driver (``robodojo.original``) both run this class, so the two sides of a
parity experiment cannot differ in prompts, parsing, validation, retries or
limits. A caller supplies only a *bridge*: how to run the bridge client and
how to fetch the files it saved.

The scaffold is deliberately small: one tool, no shell, no file or image tools
and no task-specific hints. See ``prompts.py`` for the exact prompt.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
import inspect
import json
import logging
import os
from pathlib import Path
import re
import shutil
import time
from typing import Any, Protocol
from urllib.parse import urlsplit

from . import bridge_client
from .contract import CONTRACT_VERSION, TOOL_NAME, DecisionError, hold_decision, validate_decision
from .prompts import PROMPT_SHA256, build_messages, observation_text, tool_definition

AGENT_NAME = "robodojo-direct"
AGENT_VERSION = "0.2.2"
# Seconds to wait for the simulator's bridge before giving up.
SETUP_TIMEOUT_SEC = 1740.0


class DirectRunError(RuntimeError):
    """The run cannot continue. It fails and yields no reward."""


class Bridge(Protocol):
    """How the player reaches the bridge client, wherever that client runs."""

    async def run(self, arguments: Sequence[str], timeout: float) -> tuple[int, str, str]:
        """Run the bridge client once; return its exit code, output and errors."""

    async def download(self, source_dir: str, target_dir: Path) -> None:
        """Copy a directory that the bridge client saved to this machine."""


def run_bridge_client(url: str, out: Path, arguments: Sequence[str]) -> tuple[int, str]:
    """Run the real bridge client in this process; return its exit code and output."""
    lines: list[str] = []
    try:
        code = bridge_client.main(["--url", url, "--out", str(out), *arguments], lines.append)
    except SystemExit as stop:  # Wrong usage ends the real program with a nonzero code.
        code = stop.code if type(stop.code) is int else 1
    return code, "\n".join(lines)


class LocalBridge:
    """The bridge client run in this process, for runs without Harbor or Docker."""

    def __init__(self, url: str, out: Path) -> None:
        self.url, self.out = url, Path(out)

    async def run(self, arguments: Sequence[str], timeout: float) -> tuple[int, str, str]:
        # The bridge client applies its own transport timeout to every call.
        code, output = await asyncio.to_thread(run_bridge_client, self.url, self.out, arguments)
        return code, output, ""

    async def download(self, source_dir: str, target_dir: Path) -> None:
        shutil.copytree(source_dir, target_dir, dirs_exist_ok=True)


def _get(value: Any, key: str, default: Any = None) -> Any:
    """Read a field from either a dictionary or an SDK response object."""
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def public_endpoint(url: Any) -> str | None:
    """Reduce an endpoint URL to its non-secret identity: scheme, host, port and path.

    User information, query strings and fragments can carry credentials and are
    dropped. A value without a recognizable host yields ``"unrecognized"``.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    url = url.strip()
    try:
        parts = urlsplit(url if "://" in url else "//" + url)
        host, port = (parts.hostname or "").lower(), parts.port
    except ValueError:
        return "unrecognized"
    if not host:
        return "unrecognized"
    if ":" in host:
        host = f"[{host}]"  # Preserve the IPv6 port boundary.
    scheme = f"{parts.scheme.lower()}://" if parts.scheme else ""
    return f"{scheme}{host}{f':{port}' if port else ''}{parts.path.rstrip('/')}"


def _recorded_number(value: Any) -> Any:
    """Write 600 and 600.0 the same way, so that equal limits compare equal as text."""
    return float(value) if type(value) in (int, float) else value


async def _litellm_completion(**kwargs: Any) -> Any:
    import litellm

    return await litellm.acompletion(**kwargs)


def _litellm_cost(response: Any) -> float | None:
    try:
        import litellm

        return float(litellm.completion_cost(completion_response=response))
    except Exception:  # Unknown models have no price; the caller decides what that means.
        return None


class DirectPlayer:
    def __init__(
        self,
        model_name: str | None,
        work_dir: Path,
        *,
        max_invalid_responses: int = 5,
        max_decisions_per_episode: int = 0,
        max_cost_usd: float | None = None,
        image_history: int = 1,
        history_window: int = 0,
        temperature: float | None = None,
        reasoning_effort: str | None = None,
        max_output_tokens: int | None = None,
        model_seed: int | None = None,
        tool_choice: str = "required",
        api_base: str | None = None,
        api_key_env: str | None = None,
        api_retries: int = 4,
        api_timeout_sec: float = 600.0,
        wait_sec: float = 600.0,
        max_idle_sec: float = 7200.0,
        save_images: bool = False,
        completion_fn: Callable[..., Awaitable[Any]] | None = None,
        cost_fn: Callable[[Any], float | None] | None = None,
        get_env: Callable[[str], str | None] = os.environ.get,
        logger: logging.Logger | None = None,
    ) -> None:
        if not model_name:
            raise ValueError("Direct mode needs a vision model, for example provider/model-name")
        self.model_name = model_name
        for name, value, minimum in (
            ("max_invalid_responses", max_invalid_responses, 1),
            ("max_decisions_per_episode", max_decisions_per_episode, 0),
            ("image_history", image_history, 1),
            ("history_window", history_window, 0),
            ("api_retries", api_retries, 0),
        ):
            if type(value) is not int or value < minimum:
                raise ValueError(f"{name} must be an integer of at least {minimum}")
        if max_cost_usd is not None and not max_cost_usd > 0:
            raise ValueError("max_cost_usd must be positive")
        if tool_choice not in ("required", "auto"):
            raise ValueError("tool_choice must be 'required' or 'auto'")
        if not 0 < wait_sec <= 3600:
            raise ValueError("wait_sec must be in (0, 3600]")
        self.settings = {
            "max_invalid_responses": max_invalid_responses,
            "max_decisions_per_episode": max_decisions_per_episode,
            "image_history": image_history,
            "history_window": history_window,
            "temperature": temperature,
            "reasoning_effort": reasoning_effort,
            "max_output_tokens": max_output_tokens,
            "model_seed": model_seed,
            "tool_choice": tool_choice,
        }
        # Limits can end a run early or repeat a request, but they cannot change
        # the decisions of a run that completes. They are recorded, with one
        # spelling per number, and are not part of the identity of a run.
        self.limits = {
            "max_cost_usd": _recorded_number(max_cost_usd),
            "api_retries": api_retries,
            "api_timeout_sec": _recorded_number(api_timeout_sec),
            "wait_sec": _recorded_number(wait_sec),
            "max_idle_sec": _recorded_number(max_idle_sec),
        }
        self.max_cost_usd = max_cost_usd
        self.api_base = api_base
        if api_base is not None and public_endpoint(api_base) in (None, "unrecognized"):
            raise ValueError("api_base must be a URL with a host")
        self.endpoint = self._configured_endpoint()
        # What the responses themselves report, collected while the run goes on.
        self._served: dict[str, set[str]] = {"models": set(), "providers": set(), "api_bases": set()}
        self.api_key_env = api_key_env
        self.api_retries = api_retries
        self.api_timeout_sec = api_timeout_sec
        self.wait_sec = wait_sec
        self.max_idle_sec = max_idle_sec
        self.save_images = save_images
        self._completion_fn = completion_fn or _litellm_completion
        self._cost_fn = cost_fn or _litellm_cost
        self._get_env = get_env
        self._sleep = asyncio.sleep
        self.logger = logger or logging.getLogger(__name__)
        self.stats: dict[str, Any] = {
            "episodes": 0, "decisions": 0, "model_calls": 0, "api_attempts": 0, "invalid_responses": 0,
            "fallback_holds": 0, "input_tokens": 0, "cached_tokens": 0, "output_tokens": 0,
            "cost_usd": 0.0, "cost_known": True,
        }
        self.work_dir = Path(work_dir)

    def _configured_endpoint(self) -> dict[str, Any]:
        """Where the model requests are sent, as far as the configuration shows.

        Without an explicit ``api_base`` the provider's own environment variables
        can redirect the requests, so their values are part of the identity. Only
        the usual ``<PROVIDER>_API_BASE`` and ``<PROVIDER>_BASE_URL`` names of the
        resolved provider are read; API keys are never recorded.
        """
        overrides: dict[str, str | None] = {}
        if self.api_base is None:
            provider, separator, _ = self.model_name.partition("/")
            if not separator:
                from litellm import get_llm_provider

                try:
                    _, provider, _, _ = get_llm_provider(self.model_name)
                except Exception as error:  # LiteLLM cannot route this name either.
                    raise ValueError(
                        f"Model {self.model_name!r} has no provider prefix and LiteLLM does not "
                        "know it; write it as provider/model-name"
                    ) from error
            stem = re.sub(r"[^A-Z0-9]", "_", provider.upper())
            for name in (f"{stem}_API_BASE", f"{stem}_BASE_URL"):
                if os.environ.get(name):
                    overrides[name] = public_endpoint(os.environ[name])
        return {"api_base": public_endpoint(self.api_base), "environment": overrides}

    def provenance(self) -> dict[str, Any]:
        """Everything that must be identical for two runs to be comparable.

        ``endpoint`` is the configured destination. ``served`` holds what the
        responses reported: the model identifiers that answered and, where the
        client library states them, the provider and endpoint it used.
        """
        return {
            "agent": AGENT_NAME, "agent_version": AGENT_VERSION, "model": self.model_name,
            "contract_version": CONTRACT_VERSION, "prompt_sha256": PROMPT_SHA256,
            "settings": self.settings, "endpoint": self.endpoint,
            "served": {key: sorted(values) for key, values in self._served.items()},
        }

    def summary(self) -> dict[str, Any]:
        """The record of this run: its provenance, its limits and its counters."""
        return {**self.provenance(), "limits": dict(self.limits), "stats": dict(self.stats)}

    # ----- bridge ---------------------------------------------------------

    async def _bridge(self, bridge: Bridge, *arguments: str, timeout: float = 90) -> dict[str, Any]:
        code, output, errors = await bridge.run(arguments, timeout)
        lines = [line for line in (output or "").splitlines() if line.strip()]
        try:
            value = json.loads(lines[-1])
        except (IndexError, ValueError):
            value = None
        if code != 0 or not isinstance(value, dict):
            raise DirectRunError(
                f"Bridge call {arguments[0]!r} failed with code {code}: "
                f"{value or (errors or '')[-500:]}"
            )
        if value.get("status") in ("conflict", "bad_request"):
            raise DirectRunError(f"Bridge refused {arguments[0]!r}: {value.get('error')}")
        return value

    async def _fetch(self, bridge: Bridge, result: dict[str, Any]) -> tuple[dict[str, Any], dict[str, bytes]]:
        """Copy the open observation and its camera frames to this machine."""
        observation = result["observation"]
        local = self.work_dir / "observations" / observation["request_id"]
        local.mkdir(parents=True, exist_ok=True)
        await bridge.download(result["observation_dir"], local)
        found = next(iter(local.rglob("observation.json")), None)
        if found is None:
            raise DirectRunError("Downloaded observation is incomplete")
        images = {}
        for camera in observation["cameras"]:
            path = found.parent / f"{camera}.png"
            if not path.is_file():
                raise DirectRunError(f"Downloaded observation lacks the {camera} frame")
            images[camera] = path.read_bytes()
        if not self.save_images:
            shutil.rmtree(local, ignore_errors=True)
        return observation, images

    async def _abort(self, bridge: Bridge, reason: str) -> None:
        try:
            await self._bridge(bridge, "abort", "--reason", reason)
        except Exception as error:  # The run is already failing; keep the first cause.
            self.logger.warning("Could not notify the simulator of the abort: %s", error)

    # ----- model ----------------------------------------------------------

    async def _complete(self, messages: list[dict[str, Any]]) -> Any:
        tool = tool_definition()
        arguments: dict[str, Any] = {
            "model": self.model_name, "messages": messages, "tools": [tool],
            "tool_choice": self.settings["tool_choice"], "timeout": self.api_timeout_sec,
            # One decision per turn. Providers without this switch drop it, so
            # _parse still rejects any answer that is not exactly one call.
            "parallel_tool_calls": False, "drop_params": True, "max_retries": 0,
        }
        for key, option in (
            ("temperature", "temperature"), ("reasoning_effort", "reasoning_effort"),
            ("max_tokens", "max_output_tokens"), ("seed", "model_seed"),
        ):
            if self.settings[option] is not None:
                arguments[key] = self.settings[option]
        if self.api_base:
            arguments["api_base"] = self.api_base
        if self.api_key_env:
            key = self._get_env(self.api_key_env)
            if not key:
                raise DirectRunError(f"Environment variable {self.api_key_env} is not set")
            arguments["api_key"] = key
        # Retrying is safe: physics is paused and nothing was sent to the robot.
        for attempt in range(self.api_retries + 1):
            self.stats["api_attempts"] += 1
            try:
                response = await self._completion_fn(**arguments)
                break
            except Exception as error:
                if attempt == self.api_retries:
                    raise DirectRunError(f"Model API failed after {attempt + 1} attempts: {error}") from error
                self.logger.warning("Model API error (attempt %d): %s", attempt + 1, error)
                await self._sleep(min(60, 2 ** (attempt + 1)))
        usage = _get(response, "usage")
        details = _get(usage, "prompt_tokens_details")
        hidden = _get(response, "_hidden_params")
        for key, value in (
            ("models", _get(response, "model")),
            ("providers", _get(hidden, "custom_llm_provider")),
            ("api_bases", public_endpoint(_get(hidden, "api_base"))),
        ):
            if isinstance(value, str) and value:
                self._served[key].add(value)
        self.stats["model_calls"] += 1
        self.stats["input_tokens"] += int(_get(usage, "prompt_tokens", 0) or 0)
        self.stats["output_tokens"] += int(_get(usage, "completion_tokens", 0) or 0)
        self.stats["cached_tokens"] += int(_get(details, "cached_tokens", 0) or 0)
        cost = self._cost_fn(response)
        if cost is None:
            self.stats["cost_known"] = False
        else:
            self.stats["cost_usd"] += cost
        return response

    def _check_budget(self) -> str | None:
        if self.max_cost_usd is None:
            return None
        if not self.stats["cost_known"]:
            return "max_cost_usd is set but the price of this model is unknown"
        if self.stats["cost_usd"] >= self.max_cost_usd:
            return f"spending limit of {self.max_cost_usd} USD reached"
        return None

    @staticmethod
    def _parse(response: Any) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
        """Return the history turn, the proposed decision, and a parse error."""
        choices = _get(response, "choices") or []
        message = _get(choices[0], "message") if choices else None
        text = _get(message, "content") or ""
        calls = _get(message, "tool_calls") or []
        names = [str(_get(_get(call, "function"), "name"))[:64] for call in calls]
        # Only one call of the one offered tool is a decision. Anything else is
        # answered as text, so no part of it can reach the robot.
        if names != [TOOL_NAME]:
            if not calls:
                error = "The model did not call the tool"
            elif len(calls) > 1:
                error = f"The model made {len(calls)} tool calls; exactly one is allowed"
            else:
                error = f"The model called an unknown tool: {names[0]!r}"
            return (
                {"kind": "reply", "text": text if isinstance(text, str) else "", "tool_names": names,
                 "result": f"No action was executed: {error}. Call `{TOOL_NAME}` exactly once."},
                None, error,
            )
        function = _get(calls[0], "function")
        raw = _get(function, "arguments") or ""
        turn = {
            "kind": "attempt", "call_id": _get(calls[0], "id") or "call_0",
            "arguments": raw if isinstance(raw, str) else json.dumps(raw),
            "text": text if isinstance(text, str) else "",
        }
        try:
            decision = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError as error:
            return turn, None, f"Tool arguments are not valid JSON: {error}"
        return turn, decision, None

    def _log(self, record: dict[str, Any]) -> None:
        with (self.work_dir / "decisions.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record) + "\n")

    async def _decide(self, bridge: Bridge, observation: dict[str, Any], turns: list[dict[str, Any]]) -> dict[str, Any]:
        """Ask the model until one decision is accepted, then return its outcome."""
        invalid = 0
        while True:
            started = time.monotonic()
            response = await self._complete(build_messages(
                turns, image_history=self.settings["image_history"],
                history_window=self.settings["history_window"],
            ))
            reason = self._check_budget()
            if reason is not None:
                raise DirectRunError(f"Run stopped: {reason}")
            turn, decision, error = self._parse(response)
            result = None
            if error is None:
                try:
                    decision = validate_decision(decision, observation)
                except DecisionError as failure:
                    error = str(failure)
            if error is None:
                result = await self._bridge(
                    bridge, "act", "--response", json.dumps(decision),
                    "--wait", str(self.wait_sec), timeout=self.wait_sec + 90,
                )
                if result["status"] == "rejected":
                    error = result["error"]
            self._log({
                "episode_index": observation["episode"]["episode_index"],
                "request_id": observation["request_id"], "step_id": observation["step_id"],
                "arguments": turn.get("arguments"), "tool_names": turn.get("tool_names"),
                "error": error,
                "model_seconds": round(time.monotonic() - started, 3),
            })
            if error is None:
                turn["result"] = json.dumps({"status": "accepted"})
                turns.append(turn)
                self.stats["decisions"] += 1
                return result
            invalid += 1
            self.stats["invalid_responses"] += 1
            turn.setdefault("result", json.dumps({"status": "rejected", "error": error}))
            turns.append(turn)
            if invalid >= self.settings["max_invalid_responses"]:
                # Keep the episode alive without inventing a motion.
                hold = hold_decision(
                    observation,
                    f"Scaffold hold after {invalid} invalid model responses; not a model decision.",
                )
                result = await self._bridge(
                    bridge, "act", "--response", json.dumps(hold),
                    "--wait", str(self.wait_sec), timeout=self.wait_sec + 90,
                )
                if result["status"] == "rejected":
                    await self._abort(bridge, "scaffold hold was rejected")
                    raise DirectRunError(f"Scaffold hold was rejected: {result['error']}")
                turns.append({
                    "kind": "note",
                    "text": "Your previous calls were invalid, so both arms were held still for one control step.",
                })
                self._log({
                    "episode_index": observation["episode"]["episode_index"],
                    "request_id": observation["request_id"], "scaffold_hold": True,
                })
                self.stats["fallback_holds"] += 1
                self.stats["decisions"] += 1
                return result

    # ----- run ------------------------------------------------------------

    async def setup(self, bridge: Bridge, timeout_sec: float = SETUP_TIMEOUT_SEC) -> dict[str, Any]:
        """Wait until the broker in the simulator answers."""
        self.work_dir.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout_sec
        while True:
            try:
                return await self._bridge(bridge, "status")
            except DirectRunError as error:
                if time.monotonic() >= deadline:
                    raise DirectRunError(f"Simulator bridge did not become reachable: {error}") from error
                await self._sleep(5)

    async def _episode(self, bridge: Bridge, result: dict[str, Any]) -> None:
        turns: list[dict[str, Any]] = []
        image_limit = self.settings["image_history"]
        if self.settings["history_window"]:
            image_limit = min(image_limit, self.settings["history_window"])
        recent_images: deque[dict[str, Any]] = deque()
        decisions = 0
        limit = self.settings["max_decisions_per_episode"]
        while True:
            observation, images = await self._fetch(bridge, result)
            turn = {"kind": "observation", "text": observation_text(observation), "images": images}
            turns.append(turn)
            recent_images.append(turn)
            if len(recent_images) > image_limit:
                del recent_images.popleft()["images"]
            if limit and decisions >= limit:
                reason = f"decision limit of {limit} per episode reached"
                await self._abort(bridge, reason)
                raise DirectRunError(f"Run stopped: {reason}")
            result = await self._decide(bridge, observation, turns)
            decisions += 1
            if result["status"] == "episode_end":
                self.stats["episodes"] += 1
                self._log({"episode_end": result["episode"]})
                return
            if result["status"] != "observation":
                # A slow or failed simulator leaves the outcome of the action unknown.
                await self._abort(bridge, f"unexpected outcome after an action: {result['status']}")
                raise DirectRunError(f"Unexpected outcome after an action: {result}")

    async def run(self, bridge: Bridge) -> None:
        """Play every episode until the simulator reports the end of its run."""
        self.work_dir.mkdir(parents=True, exist_ok=True)
        idle_since = time.monotonic()
        try:
            while True:
                result = await self._bridge(
                    bridge, "observe", "--wait", str(self.wait_sec), timeout=self.wait_sec + 90,
                )
                status = result["status"]
                if status == "observation":
                    await self._episode(bridge, result)  # Model history starts empty.
                    idle_since = time.monotonic()
                elif status == "finished":
                    return
                elif status == "waiting":
                    if time.monotonic() - idle_since > self.max_idle_sec:
                        await self._abort(bridge, "simulator produced no observation in time")
                        raise DirectRunError("Simulator produced no observation in time")
                else:
                    raise DirectRunError(f"Simulator run ended without completing: {result}")
        except BaseException as error:
            # Whatever went wrong, stop the native run: a simulator left waiting
            # for a decision could otherwise be mistaken for a live evaluation.
            await self._abort(bridge, f"agent stopped: {type(error).__name__}: {error}"[:500])
            raise
        finally:
            (self.work_dir / "run.json").write_text(
                json.dumps(self.summary(), indent=2) + "\n", encoding="utf-8",
            )


# Options a caller may set, with their defaults defined in one place above.
# ``get_env`` and ``logger`` connect the player to its host and are not options.
PLAYER_OPTIONS = tuple(
    name for name, parameter in inspect.signature(DirectPlayer.__init__).parameters.items()
    if parameter.kind is inspect.Parameter.KEYWORD_ONLY and name not in ("get_env", "logger")
)
