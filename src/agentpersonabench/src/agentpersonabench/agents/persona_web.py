"""Harbor agent for AgentPersonaBench web tasks.

Runs APB's ``web_agent.py`` inside the task container. The persona system
prompt is composed on the host from ``persona.yaml`` and the role in
``web.json``, then passed as ``PERSONA_SYS``. The task instruction stays the
goal file. Model calls go through a host proxy, the same split APB's
``web_solve.sh`` uses.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from agentpersonabench.agents.host_proxy import HostChatProxy
from agentpersonabench.persona_prompt import persona_system_prompt

_RUNTIME = Path(__file__).resolve().parent / "web_runtime"
_DEFAULT_MAX_STEPS = 40


class PersonaWeb(BaseAgent):
    def __init__(self, *args, max_steps: int | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._max_steps = int(max_steps) if max_steps is not None else _DEFAULT_MAX_STEPS

    @staticmethod
    def name() -> str:
        return "persona-web"

    def version(self) -> str | None:
        return "1.0.0"

    async def setup(self, environment: BaseEnvironment) -> None:
        return None

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        task_dir = Path(environment.environment_dir).parent
        persona = _persona_file(task_dir)
        spec = _web_spec(task_dir)
        role = "a user"
        if spec.is_file():
            role = json.loads(spec.read_text(encoding="utf-8")).get("role") or role
        system = persona_system_prompt(persona, role=role)
        goal = self.logs_dir / "goal.md"
        prompt = self.logs_dir / "persona_sys.txt"
        goal.write_text(instruction, encoding="utf-8")
        prompt.write_text(system, encoding="utf-8")

        if self.model_name is None:
            raise RuntimeError("persona-web requires a model name")
        os.environ.setdefault(
            "LLM_REASONING_EFFORT",
            os.environ.get("ADHERENCE_EFFORT") or "medium",
        )
        proxy = HostChatProxy(self.model_name).start()
        try:
            await environment.upload_file(_RUNTIME / "web_agent.py", "/opt/persona-web/web_agent.py")
            await environment.upload_file(
                _RUNTIME / "agent_client.py", "/opt/persona-web/agent_client.py"
            )
            await environment.upload_file(goal, "/opt/persona-web/goal.md")
            await environment.upload_file(prompt, "/opt/persona-web/persona_sys.txt")
            result = await environment.exec(
                command=(
                    "python3 -c \"import os, pathlib, sys; "
                    "os.environ['PERSONA_SYS']=pathlib.Path('/opt/persona-web/persona_sys.txt').read_text(); "
                    "os.execv(sys.executable, [sys.executable, '/opt/persona-web/web_agent.py', "
                    "'--goal-file', '/opt/persona-web/goal.md', '--spec', '/app/web.json'])\""
                ),
                cwd="/app",
                env={
                    "LLM_PROXY_URL": proxy.container_url,
                    "LLM_MODEL": proxy.model_name,
                    "WEB_MAX_STEPS": str(self._max_steps),
                    "APB_WEB_OBS": "hybrid",
                    "PYTHONPATH": "/opt/persona-web",
                },
                timeout_sec=540,
            )
        finally:
            proxy.stop()

        prompt_tokens = sum(int(call.get("prompt_tokens") or 0) for call in proxy.calls)
        completion_tokens = sum(
            int(call.get("completion_tokens") or 0) for call in proxy.calls
        )
        if prompt_tokens or completion_tokens:
            context.n_input_tokens = prompt_tokens
            context.n_output_tokens = completion_tokens
        context.metadata = {
            "agent": self.name(),
            "llm_calls": len(proxy.calls),
            "role": role,
        }
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        if stdout:
            self.logger.info(stdout)
        if result.return_code != 0:
            raise RuntimeError(
                f"persona-web exited {result.return_code}: {stderr or stdout}"
            )


def _persona_file(task_dir: Path) -> Path:
    for candidate in (task_dir / "persona.yaml", task_dir / "tests" / "persona.yaml"):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"no persona.yaml under {task_dir}")


def _web_spec(task_dir: Path) -> Path:
    for candidate in (
        task_dir / "environment" / "web.json",
        task_dir / "solution" / "web.json",
        task_dir / "tests" / "web.json",
    ):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"no web.json under {task_dir}")
