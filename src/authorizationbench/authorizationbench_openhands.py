"""Harbor agent that runs the upstream AuthorizationBench OpenHands 1.49.2 harness.

Used for parity: it reproduces the paper's OpenHands configuration (native
prompt condition, task shell-tool subset, 35 steps, 300 s, plain replies routed
to the Oracle User) against the task's sidecar. See
``run_authorizationbench_openhands.py``.

Usage (from the adapters repository root):

    PYTHONPATH=src/authorizationbench harbor run -p datasets/authorizationbench \\
        --agent-import-path authorizationbench_openhands:AuthorizationBenchOpenHands \\
        -m openai/<model>
"""
from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from harbor.agents.installed.base import BaseInstalledAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

SDK_VERSION = "1.49.2"
AUTHZ_REPO = "https://github.com/PEACH-Research-Lab/AuthorizationBench.git"
AUTHZ_COMMIT = "bfe1fc27b513d47dd02a0b5625ccb4f9a2b0e47c"
PREFIX = "/opt/authz-oh"
RUNNER = "run_authorizationbench_openhands.py"
INSTRUCTION_PATH = "/tmp/authz_instruction.md"


class AuthorizationBenchOpenHands(BaseInstalledAgent):
    def __init__(
        self,
        *args: Any,
        reasoning_effort: str | None = None,
        max_output_tokens: int | None = None,
        extra_body_json: str | None = None,
        task_timeout_sec: float = 300.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._reasoning_effort = reasoning_effort
        self._max_output_tokens = max_output_tokens
        self._extra_body_json = extra_body_json
        self._task_timeout_sec = task_timeout_sec

    @staticmethod
    def name() -> str:
        return "authorizationbench-openhands"

    def version(self) -> str | None:
        return SDK_VERSION

    async def install(self, environment: BaseEnvironment) -> None:
        sparse = " ".join(shlex.quote(p) for p in (
            "/authorization_eva_sandbox/*",
            "!/authorization_eva_sandbox/tasks/",
            "!/authorization_eva_sandbox/task_generation/",
            "!/authorization_eva_sandbox/vignettes/",
        ))
        await self.exec_as_root(environment, command=" && ".join([
            f"python3 -m venv {PREFIX}/venv",
            f"git init -q {PREFIX}/ab",
            f"cd {PREFIX}/ab",
            f"git remote add origin {AUTHZ_REPO}",
            "git config core.sparseCheckout true",
            f"printf '%s\\n' {sparse} > .git/info/sparse-checkout",
            f"git fetch -q --depth 1 --filter=blob:none origin {AUTHZ_COMMIT}",
            "git checkout -q FETCH_HEAD",
            "rm -rf .git",
            f"{PREFIX}/venv/bin/pip install --no-cache-dir -q "
            f"-r {PREFIX}/ab/authorization_eva_sandbox/requirements-core.txt "
            f"openhands-sdk=={SDK_VERSION} openhands-tools=={SDK_VERSION}",
        ]))
        local = self.logs_dir / RUNNER
        local.write_text(Path(__file__).with_name(RUNNER).read_text(encoding="utf-8"),
                         encoding="utf-8")
        await environment.upload_file(local, f"{PREFIX}/{RUNNER}")

    async def run(self, instruction: str, environment: BaseEnvironment,
                  context: AgentContext) -> None:
        if not self.model_name:
            raise ValueError("model_name is required")
        servers = [s for s in self.mcp_servers if s.name == "authorization-bench"]
        if len(servers) != 1 or not servers[0].url:
            raise ValueError("task must configure the authorization-bench MCP server")
        local = self.logs_dir / "authz_instruction.md"
        local.write_text(instruction, encoding="utf-8")
        await environment.upload_file(local, INSTRUCTION_PATH)

        parts = [f"PYTHONPATH={PREFIX}/ab:{PREFIX}/ab/authorization_eva_sandbox",
                 f"{PREFIX}/venv/bin/python", f"{PREFIX}/{RUNNER}",
                 "--instruction-file", INSTRUCTION_PATH,
                 "--mcp-url", shlex.quote(servers[0].url),
                 "--model", shlex.quote(self.model_name),
                 "--task-timeout-sec", str(self._task_timeout_sec)]
        if self._reasoning_effort:
            parts += ["--reasoning-effort", shlex.quote(self._reasoning_effort)]
        if self._max_output_tokens:
            parts += ["--max-output-tokens", str(self._max_output_tokens)]
        if self._extra_body_json:
            parts += ["--extra-body-json", shlex.quote(self._extra_body_json)]
        env = {}
        for target, sources in (("LLM_API_KEY", ("AUTHZ_AGENT_API_KEY", "LLM_API_KEY")),
                                ("LLM_BASE_URL", ("AUTHZ_AGENT_BASE_URL", "LLM_BASE_URL"))):
            for source in sources:
                value = self._get_env(source)
                if value:
                    env[target] = value
                    break
        await self.exec_as_agent(environment, command=" ".join(parts), env=env)

    def populate_context_post_run(self, context: AgentContext) -> None:
        path = self.logs_dir / "authz_cost.json"
        if not path.is_file():
            return
        try:
            combined = json.loads(path.read_text(encoding="utf-8")).get("combined") or {}
        except json.JSONDecodeError:
            return
        context.n_input_tokens = combined.get("prompt_tokens")
        context.n_output_tokens = combined.get("completion_tokens")
        context.n_cache_tokens = combined.get("cache_read_tokens")
        context.cost_usd = combined.get("accumulated_cost_usd")
