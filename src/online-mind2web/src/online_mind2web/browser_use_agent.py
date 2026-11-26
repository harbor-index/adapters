"""Browser Use as a Harbor agent for Online-Mind2Web.

Installs a pinned Browser Use into the task container, runs `run_browser_use.py` against
the task's starting website, and leaves an `online-mind2web-v2` trajectory at
`/app/output/<task_id>/` for the WebJudge verifier. A copy is kept under the trial's
agent logs (`agent/trajectory/`) for inspection and parity uploads.

Usage (from the adapters repo root):

    PYTHONPATH=src/online-mind2web/src harbor run \\
        -p datasets/online-mind2web \\
        --agent-import-path online_mind2web.browser_use_agent:BrowserUseAgent \\
        -m openai/gpt-6-luna
"""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

BROWSER_USE_VERSION = "0.13.14"
VENV = "/opt/browser-use"
RUNNER = f"{VENV}/run_browser_use.py"
TASK_JSON = "/app/task.json"  # written into the image by the adapter (task_id, website, ...)
OUTPUT_DIR = "/app/output"  # contract with tests/test.sh
LOG_COPY_DIR = "/logs/agent/trajectory"


class BrowserUseAgent(BaseInstalledAgent):
    def __init__(
        self,
        *args,
        max_steps: int = 50,
        max_actions_per_step: int = 1,
        reasoning_effort: str = "low",
        browser_use_version: str = BROWSER_USE_VERSION,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._max_steps = int(max_steps)
        self._max_actions_per_step = int(max_actions_per_step)
        self._reasoning_effort = reasoning_effort
        self._browser_use_version = browser_use_version

    @staticmethod
    def name() -> str:
        return "browser-use"

    def version(self) -> str | None:
        return self._version or self._browser_use_version

    async def install(self, environment: BaseEnvironment) -> None:
        await self.exec_as_root(
            environment,
            command=(
                f"python3 -m venv {VENV} && "
                f"{VENV}/bin/pip install --quiet --no-cache-dir "
                f"browser-use=={self._browser_use_version}"
            ),
        )
        runner_src = Path(__file__).parent / "run_browser_use.py"
        local_copy = self.logs_dir / "run_browser_use.py"
        local_copy.write_text(runner_src.read_text())
        await environment.upload_file(source_path=local_copy, target_path=RUNNER)

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        if not self.model_name:
            raise ValueError("BrowserUseAgent requires a model, e.g. -m openai/gpt-6-luna")

        instr_local = self.logs_dir / "instruction.md"
        instr_local.write_text(instruction)
        await environment.upload_file(source_path=instr_local, target_path="/tmp/instruction.md")

        env = {
            "ANONYMIZED_TELEMETRY": "false",
            "BROWSER_USE_CLOUD_SYNC": "false",
            "BROWSER_USE_LOGGING_LEVEL": "info",
        }
        for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL"):
            if os.environ.get(key):
                env[key] = os.environ[key]

        cmd = " ".join(
            [
                f"{VENV}/bin/python",
                RUNNER,
                f"--task-json {TASK_JSON}",
                "--instruction-file /tmp/instruction.md",
                f"--output-dir {OUTPUT_DIR}",
                f"--model {shlex.quote(self.model_name)}",
                f"--max-steps {self._max_steps}",
                f"--max-actions-per-step {self._max_actions_per_step}",
                f"--reasoning-effort {shlex.quote(self._reasoning_effort)}",
            ]
        )
        try:
            await self.exec_as_agent(
                environment,
                command=f"{cmd} 2>&1 | tee /logs/agent/browser_use.log",
                env=env,
            )
        finally:
            await self.exec_as_root(
                environment,
                command=f"mkdir -p {LOG_COPY_DIR} && cp -r {OUTPUT_DIR}/. {LOG_COPY_DIR}/ 2>/dev/null || true",
            )

    def populate_context_post_run(self, context: AgentContext) -> None:
        for summary_path in (self.logs_dir / "trajectory").glob("*/run_summary.json"):
            try:
                usage = json.loads(summary_path.read_text()).get("usage") or {}
            except (OSError, json.JSONDecodeError):
                continue
            context.n_input_tokens = usage.get("total_prompt_tokens")
            context.n_output_tokens = usage.get("total_completion_tokens")
            context.cost_usd = usage.get("total_cost")
