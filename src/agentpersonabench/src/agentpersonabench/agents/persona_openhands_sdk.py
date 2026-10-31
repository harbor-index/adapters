"""Host launcher for the survey parity agent.

This class does not call the model. It reads ``persona.yaml`` from the host
task directory, composes the instruction APB's unified survey harness sends,
and then runs OpenHands SDK inside the task container. It does not upload
``persona.yaml``.

Two behaviors live in APB's OpenHands fork and not in stock Harbor. This class
puts them back: ``reasoning_effort`` (default ``medium``) is passed into the
SDK call, and non-model HTTP is pointed at a dead proxy unless
``APB_SURVEY_OFFLINE=0``.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

import harbor.agents.installed.openhands_sdk as stock_openhands
from harbor.agents.installed.openhands_sdk import OpenHandsSDK
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from agentpersonabench.persona_jinja import render_persona_instruction
from agentpersonabench.persona_prompt import persona_system_prompt

_EFFORT_ANCHOR = (
    '    if temperature_raw:\n'
    '        llm_kwargs["temperature"] = float(temperature_raw)\n'
)
_EFFORT_BLOCK = (
    '    effort = (os.environ.get("LLM_REASONING_EFFORT") or "").strip().lower()\n'
    "    if effort:\n"
    '        llm_kwargs["reasoning_effort"] = effort\n'
)
_MODEL_API_HOSTS = (
    "googleapis.com",
    "openai.com",
    "anthropic.com",
    "azure.com",
    "ppapi.ai",
    "z.ai",
    "aliyuncs.com",
    "x.ai",
    "deepseek.com",
    "openrouter.ai",
    "localhost",
    "127.0.0.1",
)


def runner_with_reasoning_effort(source: str) -> str:
    """Teach Harbor's runner to read ``LLM_REASONING_EFFORT``."""
    if "LLM_REASONING_EFFORT" in source:
        return source
    if _EFFORT_ANCHOR not in source:
        raise RuntimeError(
            "Harbor's openhands_sdk_runner.py has no temperature block to patch"
        )
    return source.replace(_EFFORT_ANCHOR, _EFFORT_ANCHOR + _EFFORT_BLOCK, 1)


class PersonaOpenHandsSdk(OpenHandsSDK):
    @staticmethod
    def name() -> str:
        return "persona-openhands-sdk"

    def version(self) -> str | None:
        return "1.0.0"

    def __init__(
        self,
        *args,
        reasoning_effort: str | None = "medium",
        persona_path: str | None = None,
        **kwargs,
    ) -> None:
        self._persona_path = persona_path
        super().__init__(*args, reasoning_effort=reasoning_effort, **kwargs)

    def render_instruction(self, instruction: str) -> str:
        instruction = super().render_instruction(instruction)
        task_dir = self._task_dir()
        task_text = instruction.rstrip() + "\n\n" + persona_system_prompt(task_dir)
        return render_persona_instruction(task_dir / "persona.yaml", task_text)

    async def install(self, environment: BaseEnvironment) -> None:
        await super().install(environment)
        runner_path = Path(stock_openhands.__file__).resolve().parent / "openhands_sdk_runner.py"
        patched = runner_with_reasoning_effort(runner_path.read_text(encoding="utf-8"))
        local_copy = self.logs_dir / "run_agent.py"
        local_copy.write_text(patched, encoding="utf-8")
        await environment.upload_file(
            source_path=local_copy,
            target_path="/installed-agent/run_agent.py",
        )
        await environment.exec(
            command="chmod +x /installed-agent/run_agent.py",
            user="root",
        )

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        original = self.exec_as_agent

        async def exec_as_agent(*args, **kwargs):
            env = dict(kwargs.get("env") or {})
            env.update(self._survey_process_env(env))
            kwargs["env"] = env
            return await original(*args, **kwargs)

        self.exec_as_agent = exec_as_agent
        try:
            await super().run(instruction, environment, context)
        finally:
            self.exec_as_agent = original

    def _survey_process_env(self, env: dict[str, str]) -> dict[str, str]:
        extra: dict[str, str] = {}
        if self._reasoning_effort:
            extra["LLM_REASONING_EFFORT"] = str(self._reasoning_effort)
        raw = self._get_env("APB_SURVEY_OFFLINE")
        offline = raw is None or raw.strip().lower() not in {"0", "false", "no"}
        if not offline:
            return extra
        allow = list(_MODEL_API_HOSTS)
        base = env.get("LLM_BASE_URL") or self._get_env("LLM_BASE_URL") or ""
        hostname = urlparse(base).hostname if base else None
        if hostname:
            allow.append(hostname)
        dead = "http://127.0.0.1:9"
        for name in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "http_proxy",
            "https_proxy",
            "ALL_PROXY",
            "all_proxy",
        ):
            extra[name] = dead
        extra["NO_PROXY"] = extra["no_proxy"] = ",".join(allow)
        return extra

    def _task_dir(self) -> Path:
        if self._persona_path:
            path = Path(self._persona_path).expanduser()
            if path.is_file():
                return path.parent
            if (path / "persona.yaml").is_file():
                return path
            raise FileNotFoundError(f"no persona.yaml at {path}")

        config_path = self.logs_dir.parent / "config.json"
        if not config_path.is_file():
            raise FileNotFoundError(
                "persona_path was not passed and trial config.json is missing "
                f"at {config_path}"
            )
        data = json.loads(config_path.read_text(encoding="utf-8"))
        task = data.get("task") if isinstance(data, dict) else None
        raw = None
        if isinstance(task, dict):
            raw = task.get("path") or task.get("source")
        if not raw:
            raise FileNotFoundError(f"{config_path} has no task.path")
        task_dir = Path(str(raw))
        if not (task_dir / "persona.yaml").is_file():
            raise FileNotFoundError(f"no persona.yaml in task dir {task_dir}")
        return task_dir
