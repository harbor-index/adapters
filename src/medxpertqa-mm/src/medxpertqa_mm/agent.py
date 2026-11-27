"""Harbor agent that answers one MedXpertQA-MM question the way the original harness does.

MedXpertQA is not an agentic benchmark: upstream sends the question and its images to a
chat-completion API in two turns (zero-shot CoT) and extracts the option letter from the
second response. This agent reproduces those two requests (see ``llm_request.py``) and
writes the second response to ``/app/answer.txt``, where the task verifier applies the
original answer extraction.

Usage (from the repository root)::

    export OPENAI_API_KEY=...            # and OPENAI_BASE_URL for another endpoint
    PYTHONPATH=src/medxpertqa-mm/src harbor run -p datasets/medxpertqa-mm-parity \
        -a medxpertqa_mm.agent:MedXpertQALLMAgent -m <model id> --max-retries 2

Agent kwargs (``--ak key=value``):
    api_base             OpenAI-compatible base URL (default: $OPENAI_BASE_URL or
                         https://api.openai.com/v1)
    api_key_env          environment variable holding the API key (default: OPENAI_API_KEY)
    temperature          sampling temperature (default: 0.0, as upstream's run.sh)
    request_timeout_sec  time allowed for one request (default: 600, the openai SDK default)
    drop_params          comma-separated request fields to omit, e.g. "logprobs,seed"
    token_param          send the token limit under this name instead
                         (max_tokens or max_completion_tokens)

The model sees the question and the images and nothing else: no tools, no shell.

Failures follow upstream, with one exception. A provider refusal (``content_filter`` or
``bad_response_status_code`` in the error) is answered with upstream's "No answer
provided: ..." string and scored as incorrect, as upstream does. A request that still
fails after upstream's 10 attempts raises ``ModelRequestError`` and the trial ends without
a reward, where upstream would record "No answer provided." and score it as incorrect;
``harbor run --max-retries N`` runs such trials again.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
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

from .llm_request import (
    ANSWER_PATH_IN_ENV,
    IMAGE_DIR_IN_ENV,
    NO_ANSWER_BAD_RESPONSE,
    NO_ANSWER_CONTENT_FILTER,
    UPSTREAM_ATTEMPTS,
    UPSTREAM_TEMPERATURE,
    build_payload,
    cot_trigger,
    first_turn_messages,
    parse_instruction,
    second_turn_messages,
    uses_reasoning_branch,
)

DEFAULT_API_BASE = "https://api.openai.com/v1"
DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"
DEFAULT_REQUEST_TIMEOUT_SEC = 600.0


class ModelRequestError(RuntimeError):
    """A model request kept failing (transport error, timeout or API error)."""


def _redact_images(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy of ``messages`` without the base64 image payloads, for the logs."""
    redacted = []
    for message in messages:
        content = message["content"]
        if isinstance(content, list):
            content = [
                part
                if part.get("type") != "image_url"
                else {
                    "type": "image_url",
                    "image_url": {"url": "<base64 image omitted>"},
                }
                for part in content
            ]
        redacted.append({**message, "content": content})
    return redacted


class MedXpertQALLMAgent(BaseAgent):
    """Two-request agent mirroring MedXpertQA's ``zero_shot_cot`` with ``APIAgent``."""

    capabilities = AgentCapabilities(atif=True)

    @staticmethod
    def name() -> str:
        return "medxpertqa-llm"

    def version(self) -> str | None:
        return "0.1.0"

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        *,
        api_base: str | None = None,
        api_key_env: str | None = None,
        temperature: float | str = UPSTREAM_TEMPERATURE,
        request_timeout_sec: float | str = DEFAULT_REQUEST_TIMEOUT_SEC,
        drop_params: str | None = None,
        token_param: str | None = None,
        **kwargs: Any,
    ):
        super().__init__(logs_dir=logs_dir, model_name=model_name, **kwargs)
        if not model_name:
            raise ValueError("MedXpertQALLMAgent requires a model (-m <model id>)")
        # Accept the LiteLLM-style "openai/<id>" as well as the bare id.
        self._model_id = model_name.removeprefix("openai/")
        self._api_base = api_base
        self._api_key_env = api_key_env or DEFAULT_API_KEY_ENV
        self._temperature = float(temperature)
        self._request_timeout_sec = float(request_timeout_sec)
        self._drop_params = tuple(
            p.strip() for p in (drop_params or "").split(",") if p.strip()
        )
        if token_param not in (None, "", "max_tokens", "max_completion_tokens"):
            raise ValueError("token_param must be max_tokens or max_completion_tokens")
        self._token_param = token_param or None

    async def setup(self, environment: BaseEnvironment) -> None:
        return

    # -- request -----------------------------------------------------------------

    def _resolved_api_base(self) -> str:
        base = self._api_base or self._get_env("OPENAI_BASE_URL") or DEFAULT_API_BASE
        return base.rstrip("/")

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        api_key = self._get_env(self._api_key_env)
        if not api_key:
            raise RuntimeError(
                f"API key required: set {self._api_key_env} "
                "(or pass --ak api_key_env=<VAR>)"
            )
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self._request_timeout_sec) as client:
            response = await client.post(
                f"{self._resolved_api_base()}/chat/completions",
                headers=headers,
                json=payload,
            )
        if response.status_code != 200:
            raise ModelRequestError(
                f"API error {response.status_code}: {response.text[:2000]}"
            )
        return response.json()

    async def _get_response(
        self, messages: list[dict[str, Any]], raw_log: list[dict[str, Any]]
    ) -> tuple[str, dict[str, Any]]:
        """``APIAgent.get_response``: up to 10 attempts, 1 s apart."""
        payload = build_payload(
            self._model_id,
            messages,
            temperature=self._temperature,
            drop_params=self._drop_params,
            token_param=self._token_param,
        )
        reasoning_branch = uses_reasoning_branch(self._model_id)
        last_error: Exception | None = None
        for _ in range(UPSTREAM_ATTEMPTS):
            try:
                data = await self._post(payload)
                content = data["choices"][0]["message"]["content"]
                if not isinstance(content, str):
                    raise ModelRequestError(
                        f"Response has no text content: {json.dumps(data)[:1000]}"
                    )
                raw_log.append(
                    {
                        **data,
                        "choices": [
                            {k: v for k, v in choice.items() if k != "logprobs"}
                            for choice in data["choices"]
                        ],
                    }
                )
                return content.strip(), data.get("usage") or {}
            except (
                ModelRequestError,
                httpx.HTTPError,
                KeyError,
                IndexError,
                TypeError,
                ValueError,
            ) as exc:
                last_error = exc
                text = str(exc)
                if not reasoning_branch and "bad_response_status_code" in text:
                    raw_log.append({"error": text})
                    return NO_ANSWER_BAD_RESPONSE, {}
                if not reasoning_branch and "content_filter" in text:
                    raw_log.append({"error": text})
                    return NO_ANSWER_CONTENT_FILTER, {}
                await asyncio.sleep(1)
        raise ModelRequestError(
            f"Request failed {UPSTREAM_ATTEMPTS} times: "
            f"{type(last_error).__name__}: {last_error}"
        )

    # -- task inputs and outputs -------------------------------------------------

    async def _read_images(
        self, environment: BaseEnvironment, image_names: list[str]
    ) -> list[bytes]:
        host_dir = self.logs_dir / "images"
        host_dir.mkdir(parents=True, exist_ok=True)
        images = []
        for name in image_names:
            target = host_dir / name
            await environment.download_file(f"{IMAGE_DIR_IN_ENV}/{name}", target)
            images.append(target.read_bytes())
            target.unlink()  # the image is part of the task; no need to keep a copy
        host_dir.rmdir()
        return images

    async def _write_answer(self, environment: BaseEnvironment, answer: str) -> None:
        host_path = self.logs_dir / "answer.txt"
        host_path.write_bytes(answer.encode("utf-8", errors="replace"))
        await environment.upload_file(
            source_path=host_path, target_path=ANSWER_PATH_IN_ENV
        )

    def _write_trajectory(
        self,
        turns: list[tuple[str, str, str, dict[str, Any]]],
        totals: dict[str, int],
    ) -> None:
        try:
            steps = []
            for user_text, timestamp, response, usage in turns:
                steps.append(
                    Step(
                        step_id=len(steps) + 1,
                        timestamp=timestamp,
                        source="user",
                        message=user_text,
                    )
                )
                steps.append(
                    Step(
                        step_id=len(steps) + 1,
                        timestamp=timestamp,
                        source="agent",
                        model_name=self._model_id,
                        message=response,
                        metrics=Metrics(
                            prompt_tokens=usage.get("prompt_tokens"),
                            completion_tokens=usage.get("completion_tokens"),
                        ),
                    )
                )
            trajectory = Trajectory(
                session_id=self.session_id,
                agent=AtifAgent(
                    name=self.name(),
                    version=self.version() or "unknown",
                    model_name=self._model_id,
                ),
                steps=steps,
                final_metrics=FinalMetrics(
                    total_prompt_tokens=totals["prompt_tokens"],
                    total_completion_tokens=totals["completion_tokens"],
                    total_steps=len(steps),
                ),
            )
            (self.logs_dir / "trajectory.json").write_text(
                format_trajectory_json(trajectory.to_json_dict()), encoding="utf-8"
            )
        except (ValueError, TypeError, OSError) as exc:
            # The trajectory is for inspection only; never fail a run over it.
            self.logger.warning("Failed to write trajectory.json: %s", exc)

    # -- run ---------------------------------------------------------------------

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        task = parse_instruction(instruction)
        images = await self._read_images(environment, task.image_names)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        raw_responses: list[dict[str, Any]] = []

        # Stage 1: question + images, "Let's think step by step."
        messages = first_turn_messages(task.prompt, images)
        started = datetime.now(UTC).isoformat()
        rationale, usage_1 = await self._get_response(messages, raw_responses)

        # Stage 2: "Therefore, among A through E, the answer is"
        trigger = cot_trigger(self._model_id, task.start, task.end)
        messages = second_turn_messages(messages, rationale, trigger)
        middle = datetime.now(UTC).isoformat()
        answer, usage_2 = await self._get_response(messages, raw_responses)

        await self._write_answer(environment, answer)

        (self.logs_dir / "conversation.json").write_text(
            json.dumps(
                {
                    "url": f"{self._resolved_api_base()}/chat/completions",
                    "request_parameters": {
                        k: v
                        for k, v in build_payload(
                            self._model_id,
                            [],
                            temperature=self._temperature,
                            drop_params=self._drop_params,
                            token_param=self._token_param,
                        ).items()
                        if k != "messages"
                    },
                    "messages": _redact_images(
                        [*messages, {"role": "assistant", "content": answer}]
                    ),
                    "raw_responses": raw_responses,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        totals = {
            key: (usage_1.get(key) or 0) + (usage_2.get(key) or 0)
            for key in ("prompt_tokens", "completion_tokens")
        }
        context.n_input_tokens = totals["prompt_tokens"]
        context.n_output_tokens = totals["completion_tokens"]
        context.metadata = {
            "model": self._model_id,
            "served_model": next(
                (r.get("model") for r in raw_responses if r.get("model")), None
            ),
            "n_images": len(images),
            "rationale_chars": len(rationale),
            "answer": answer[:200],
        }
        self._write_trajectory(
            [
                (task.prompt, started, rationale, usage_1),
                (trigger, middle, answer, usage_2),
            ],
            totals,
        )
