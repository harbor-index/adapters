"""Harbor agent that runs one CoT-Control instance the way upstream does.

CoT-Control-QA is not an agentic benchmark: upstream sends each prompt to the model in a
single chat-completion request and grades the *reasoning trace* the API returns next to
the answer. This agent reproduces that request (``CoT-Control-QA/llm.py::OpenRouterLLM``
with the settings of ``run_cceval.py``) and stores the reasoning trace and the final
response where the task verifier reads them.

Usage::

    export OPENROUTER_API_KEY=...
    PYTHONPATH=src/cot-control/src harbor run -p datasets/cot-control \
        -a cot_control.agent:CoTControlLLMAgent -m openai/gpt-oss-120b

Agent kwargs (``--ak key=value``):
    api_base             OpenAI-compatible base URL (default: https://openrouter.ai/api/v1)
    api_key_env          Environment variable holding the API key (default: OPENROUTER_API_KEY)
    max_tokens           Completion token limit (default: 25000, as upstream)
    reasoning_tokens     Reasoning token budget (default: 25000, as upstream)
    temperature          Sampling temperature (default: 0.0, as upstream)
    reasoning            auto | on | off. ``auto`` (default) requests reasoning only for
                         the models upstream treats as reasoning models, i.e. names
                         containing r1, thinking, qwen3, magistral or oss.
    request_timeout_sec  Total time allowed for the request (default: 300, as upstream)
    extra_body           JSON object merged into the request body, e.g. to pin an
                         OpenRouter provider: '{"provider": {"order": ["X"]}}'

The model sees the task instruction and nothing else; it has no tools and no access to
the task container.

A request that fails or exceeds ``request_timeout_sec`` raises ``ModelRequestError`` and
the trial ends without a reward. Upstream sends every prompt once and records a failed
request as an ``ERROR:`` row; ``harbor run --max-retries N`` runs such trials again.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import httpx
from harbor.agents.base import BaseAgent
from harbor.agents.capabilities import AgentCapabilities
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.trajectories.agent import Agent as AtifAgent
from harbor.models.trajectories.final_metrics import FinalMetrics
from harbor.models.trajectories.metrics import Metrics
from harbor.models.trajectories.step import Step
from harbor.models.trajectories.trajectory import Trajectory
from harbor.utils.trajectory_utils import format_trajectory_json

from .constants import REASONING_FILENAME, RESPONSE_FILENAME, TRACE_DIR_IN_ENV
from .request import (
    DEFAULT_API_BASE,
    DEFAULT_API_KEY_ENV,
    REASONING_MODES,
    UPSTREAM_MAX_TOKENS,
    UPSTREAM_REASONING_TOKENS,
    UPSTREAM_REQUEST_TIMEOUT_SEC,
    UPSTREAM_TEMPERATURE,
    build_payload,
    parse_response,
)


class ModelRequestError(RuntimeError):
    """The model request failed: transport error, timeout, or a non-200 answer.

    Upstream records such a request as a row whose response starts with ``ERROR:``. Here it
    fails the trial under this name. Harbor would otherwise report the request timeout as
    its own ``AgentTimeoutError`` (with the task's agent timeout in the message), which it
    never retries; this error is retried by ``harbor run --max-retries N``.
    """


class CoTControlLLMAgent(BaseAgent):
    """Single-request agent mirroring CoT-Control-QA's ``OpenRouterLLM``."""

    capabilities = AgentCapabilities(atif=True)

    @staticmethod
    def name() -> str:
        return "cot-control-llm"

    def version(self) -> str | None:
        return "0.1.0"

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        *,
        api_base: str | None = None,
        api_key_env: str | None = None,
        max_tokens: int | str = UPSTREAM_MAX_TOKENS,
        reasoning_tokens: int | str = UPSTREAM_REASONING_TOKENS,
        temperature: float | str = UPSTREAM_TEMPERATURE,
        reasoning: str = "auto",
        request_timeout_sec: float | str = UPSTREAM_REQUEST_TIMEOUT_SEC,
        extra_body: str | dict[str, Any] | None = None,
        **kwargs: Any,
    ):
        super().__init__(logs_dir=logs_dir, model_name=model_name, **kwargs)
        if not model_name:
            raise ValueError("CoTControlLLMAgent requires a model (-m <model id>)")

        # Accept LiteLLM-style "openrouter/<id>" as well as the bare OpenRouter id.
        self._model_id = model_name.removeprefix("openrouter/")
        self._api_base = (api_base or DEFAULT_API_BASE).rstrip("/")
        self._api_key_env = api_key_env or DEFAULT_API_KEY_ENV
        self._max_tokens = int(max_tokens)
        self._reasoning_tokens = int(reasoning_tokens)
        self._temperature = float(temperature)
        self._reasoning = str(reasoning).lower()
        if self._reasoning not in REASONING_MODES:
            raise ValueError(
                f"reasoning must be one of {REASONING_MODES}, got {reasoning!r}"
            )
        self._request_timeout_sec = float(request_timeout_sec)
        if isinstance(extra_body, str):
            extra_body = json.loads(extra_body) if extra_body.strip() else None
        if extra_body is not None and not isinstance(extra_body, dict):
            raise ValueError("extra_body must be a JSON object")
        self._extra_body: dict[str, Any] | None = extra_body

    async def setup(self, environment: BaseEnvironment) -> None:
        return

    # -- request -----------------------------------------------------------------

    async def _chat_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        api_key = self._get_env(self._api_key_env)
        if not api_key:
            raise RuntimeError(
                f"API key required: set {self._api_key_env} (or pass "
                "--ak api_key_env=<VAR>)"
            )
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        timeout = httpx.Timeout(self._request_timeout_sec)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                # Upstream limits the whole request, not individual socket reads.
                response = await asyncio.wait_for(
                    client.post(
                        f"{self._api_base}/chat/completions",
                        headers=headers,
                        json=payload,
                    ),
                    timeout=self._request_timeout_sec,
                )
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise ModelRequestError(
                f"Request timed out after {self._request_timeout_sec:g} s"
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelRequestError(f"{type(exc).__name__}: {exc}") from exc
        if response.status_code != 200:
            raise ModelRequestError(
                f"API error {response.status_code}: {response.text[:2000]}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ModelRequestError(
                f"API answer is not JSON: {response.text[:2000]}"
            ) from exc

    # -- outputs -----------------------------------------------------------------

    async def _store_trace(
        self, environment: BaseEnvironment, reasoning: str, response: str
    ) -> None:
        """Write the trace to the agent log dir and make it visible to the verifier."""
        host_dir = self.logs_dir / PurePosixPath(TRACE_DIR_IN_ENV).name
        host_dir.mkdir(parents=True, exist_ok=True)
        files = {REASONING_FILENAME: reasoning, RESPONSE_FILENAME: response}
        for filename, text in files.items():
            (host_dir / filename).write_bytes(text.encode("utf-8", errors="replace"))

        if environment.is_path_mounted(TRACE_DIR_IN_ENV, host_dir):
            return
        result = await environment.exec(command=f"mkdir -p {TRACE_DIR_IN_ENV}")
        if result.return_code != 0:
            raise RuntimeError(
                f"Could not create {TRACE_DIR_IN_ENV} in the environment: "
                f"{result.stderr or result.stdout}"
            )
        for filename in files:
            await environment.upload_file(
                source_path=host_dir / filename,
                target_path=f"{TRACE_DIR_IN_ENV}/{filename}",
            )

    def _write_trajectory(
        self,
        instruction: str,
        reasoning: str,
        response: str,
        usage: dict[str, Any],
        started_at: str,
        finished_at: str,
    ) -> None:
        try:
            metrics = Metrics(
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                cost_usd=usage.get("cost"),
            )
            trajectory = Trajectory(
                session_id=self.session_id,
                agent=AtifAgent(
                    name=self.name(),
                    version=self.version() or "unknown",
                    model_name=self._model_id,
                ),
                steps=[
                    Step(
                        step_id=1,
                        timestamp=started_at,
                        source="user",
                        message=instruction,
                    ),
                    Step(
                        step_id=2,
                        timestamp=finished_at,
                        source="agent",
                        model_name=self._model_id,
                        message=response,
                        reasoning_content=reasoning or None,
                        metrics=metrics,
                    ),
                ],
                final_metrics=FinalMetrics(
                    total_prompt_tokens=usage.get("prompt_tokens"),
                    total_completion_tokens=usage.get("completion_tokens"),
                    total_cost_usd=usage.get("cost"),
                    total_steps=2,
                ),
            )
            (self.logs_dir / "trajectory.json").write_text(
                format_trajectory_json(trajectory.to_json_dict()), encoding="utf-8"
            )
        except (ValueError, TypeError, OSError) as exc:
            # The trajectory is a convenience for inspection; never fail a run over it.
            self.logger.warning("Failed to write trajectory.json: %s", exc)

    # -- run ---------------------------------------------------------------------

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        # Upstream sends the prompt as the only message (no system prompt).
        messages = [{"role": "user", "content": instruction}]
        payload = build_payload(
            model=self._model_id,
            messages=messages,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            reasoning_tokens=self._reasoning_tokens,
            reasoning=self._reasoning,
            extra_body=self._extra_body,
        )
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.logs_dir / "request.json").write_text(
            json.dumps(
                {"url": f"{self._api_base}/chat/completions", "payload": payload},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        started_at = datetime.now(UTC).isoformat()
        start = time.monotonic()
        data = await self._chat_completion(payload)
        latency_ms = (time.monotonic() - start) * 1000
        finished_at = datetime.now(UTC).isoformat()

        (self.logs_dir / "response.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        try:
            content, reasoning = parse_response(data)
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                f"Unexpected API response (no choices/message): {json.dumps(data)[:2000]}"
            ) from exc

        # Upstream records `response.reasoning or ""` and the raw content.
        reasoning_text = reasoning if isinstance(reasoning, str) else ""
        response_text = content if isinstance(content, str) else ""
        await self._store_trace(environment, reasoning_text, response_text)

        usage = data.get("usage") or {}
        context.n_input_tokens = usage.get("prompt_tokens")
        context.n_output_tokens = usage.get("completion_tokens")
        if isinstance(usage.get("cost"), (int, float)):
            context.cost_usd = float(usage["cost"])
        context.metadata = {
            "model": self._model_id,
            "api_base": self._api_base,
            "latency_ms": latency_ms,
            "reasoning_requested": "reasoning" in payload,
            "reasoning_chars": len(reasoning_text),
            "response_chars": len(response_text),
            "finish_reason": (data.get("choices") or [{}])[0].get("finish_reason"),
            "provider": data.get("provider"),
        }
        self._write_trajectory(
            instruction, reasoning_text, response_text, usage, started_at, finished_at
        )
