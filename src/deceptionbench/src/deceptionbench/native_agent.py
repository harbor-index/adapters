"""Native single-turn API protocol for DeceptionBench, without CLI prompt additions.

The request, cleanup, and outer retry schedule match upstream ``utils.py`` at
8f3175f66fdff89ded3ebdf0ee2055fa47c955db. API failures raise a Harbor agent error
instead of submitting the upstream ``Error: ...`` placeholder for grading.
Model IDs are passed to the configured endpoint unchanged.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import shlex
from typing import Any

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from openai import AsyncOpenAI

from .protocol import normalize_response

UPSTREAM_COMMIT = "8f3175f66fdff89ded3ebdf0ee2055fa47c955db"
PARAMETERS = {"max_tokens": 500, "temperature": 0.0, "top_p": 0.95}


class DeceptionBenchAgent(BaseAgent):
    """Read only the public native prompt and submit one API response."""

    @staticmethod
    def name() -> str:
        return "deceptionbench-native"

    def version(self) -> str:
        return "0.1.0"

    async def setup(self, environment: BaseEnvironment) -> None:
        """No model credentials are installed inside the task container."""

    def _record(self, record: dict[str, Any], context: AgentContext) -> None:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        (self.logs_dir / "native_response.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        context.metadata = {
            key: value for key, value in record.items() if key != "response"
        }

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        # instruction.md contains CLI delivery instructions. The public prompt file
        # already has the upstream suffix and is the native protocol's sole input.
        del instruction
        api_key = self._get_env("DECEPTIONBENCH_MODEL_API_KEY")
        if not api_key:
            raise RuntimeError("DECEPTIONBENCH_MODEL_API_KEY is required")
        if not self.model_name:
            raise RuntimeError("An endpoint-native model_name is required")
        base_url = (
            self._get_env("DECEPTIONBENCH_MODEL_BASE_URL")
            or "https://api.openai.com/v1"
        )

        clear = await environment.exec("rm -f -- /workspace/response.txt")
        if clear.return_code != 0:
            raise RuntimeError("Cannot clear the native response artifact")
        loaded = await environment.exec("cat -- /workspace/prompt.txt")
        if loaded.return_code != 0 or not loaded.stdout or not loaded.stdout.strip():
            raise RuntimeError("Cannot read a nonempty /workspace/prompt.txt")
        prompt = loaded.stdout
        record: dict[str, Any] = {
            "status": "running",
            "model": self.model_name,
            "parameters": PARAMETERS,
            "upstream_commit": UPSTREAM_COMMIT,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "sdk_max_retries": 2,
            "outer_max_attempts": 3,
            "attempts": 0,
            "error_types": [],
        }
        self._record(record, context)
        async with AsyncOpenAI(
            api_key=api_key, base_url=base_url, max_retries=2, timeout=120.0
        ) as client:
            for attempt in range(3):
                record["attempts"] = attempt + 1
                try:
                    completion = await client.chat.completions.create(
                        model=self.model_name,
                        messages=[{"role": "user", "content": prompt}],
                        **PARAMETERS,
                    )
                    response = normalize_response(completion.choices[0].message.content)
                except Exception as exc:  # noqa: BLE001 - match upstream retry behavior
                    # Provider exception strings may contain credentials or URLs.
                    record["error_types"].append(type(exc).__name__)
                    self._record(record, context)
                    if attempt < 2:
                        await asyncio.sleep(2**attempt)
                        continue
                    record["status"] = "api_error"
                    self._record(record, context)
                    raise RuntimeError(
                        "Native API call failed after 3 attempts"
                    ) from None
                break

        usage = completion.usage
        if usage is not None:
            context.n_input_tokens = usage.prompt_tokens
            context.n_output_tokens = usage.completion_tokens
            details = getattr(usage, "prompt_tokens_details", None)
            context.n_cache_tokens = getattr(details, "cached_tokens", None)

        encoded = base64.b64encode(response.encode("utf-8")).decode("ascii")
        writer = (
            "import base64,pathlib; "
            f"pathlib.Path('/workspace/response.txt').write_bytes(base64.b64decode('{encoded}'))"
        )
        written = await environment.exec("python -I -c " + shlex.quote(writer))
        if written.return_code != 0:
            record["status"] = "artifact_error"
            self._record(record, context)
            raise RuntimeError("Cannot write the native response artifact")
        record.update(status="completed", response=response)
        self._record(record, context)
