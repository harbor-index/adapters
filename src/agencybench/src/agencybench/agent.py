"""Custom Harbor BaseAgent implementing AgencyBench V2's trusted multi-stage protocol.

Usage (Stage D/E parity & multi-stage runs):
    harbor run -p /path/to/generated/agencybench \
        --agent-import-path agencybench.agent:AgencyBenchAgent \
        --agent-kwarg upstream_root=/path/to/AgencyBench \
        --agent-kwarg inner_agent=sii \
        -m <model-id>

Security / trust boundary:
- Runs on the Harbor host outside the agent-writable container.
- Keeps `description.json` (future stage prompts) and `eval_task.py` on the host.
- Syncs ONLY the current stage workspace into container `/workspace`, executes the
  configured inner agent (`sii`, `claude-code`, `codex`, or `fixture`), pulls back
  the resulting deliverables cleanly (clearing stale host files so deletions/moves
  propagate accurately), runs the pinned upstream evaluator on the host, and
  uploads winning stage snapshots into `/tmp/.agencybench_stages/subtask{k}`
  AFTER the inner agent finishes so Harbor's `tests/test.sh` independently replays
  and verifies every stage.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import tempfile
from pathlib import Path
from typing import Any

from .controller import AgentTransport, ScenarioController
from .registry import SCENARIO_BY_TASK_ID
from .sandbox_io import clear_directory

try:
    from harbor.agents.base import BaseAgent
    from harbor.environments.base import BaseEnvironment
    from harbor.models.agent.context import AgentContext
except ImportError:  # Allow importing module for offline inspection/unit tests
    BaseAgent = object  # type: ignore[misc,assignment]
    BaseEnvironment = Any  # type: ignore[misc,assignment]
    AgentContext = Any  # type: ignore[misc,assignment]

# Allow-listed environment variables forwarded from host into the inner agent
# when present (explicit BaseAgent extra_env always takes precedence).
FORWARDED_ENV_VARS: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "SII_AGENT_API_KEY",
    "SII_AGENT_API_BASE_URL",
    "SII_TARGET_MODEL",
    "SII_AUTH_TYPE",
    "SII_MAX_TURNS",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)


class HarborContainerTransport:
    """Bridges host-side ScenarioController to an inner agent inside a Harbor container."""

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        environment: BaseEnvironment,
        inner_agent: str,
        model_name: str | None,
        extra_env: dict[str, str] | None = None,
        fixture_cmd: str | None = None,
    ) -> None:
        self._loop = loop
        self._env = environment
        self._inner_agent = inner_agent
        self._model_name = model_name
        self._extra_env = dict(extra_env or {})
        self._fixture_cmd = fixture_cmd

    def send(
        self,
        *,
        subtask: str,
        attempt: int,
        prompt: str,
        workspace_notice: str,
        workspace: Path,
    ) -> str:
        return asyncio.run_coroutine_threadsafe(
            self._send_async(
                subtask=subtask,
                attempt=attempt,
                prompt=prompt,
                workspace_notice=workspace_notice,
                workspace=workspace,
            ),
            self._loop,
        ).result()

    async def _send_async(
        self,
        *,
        subtask: str,
        attempt: int,
        prompt: str,
        workspace_notice: str,
        workspace: Path,
    ) -> str:
        # 1. Reset container /workspace and upload current stage input state.
        await self._env.exec(
            "rm -rf /workspace/* /workspace/.[!.]* /workspace/..?* 2>/dev/null || true"
        )
        if any(workspace.iterdir()):
            await self._env.upload_dir(str(workspace), "/workspace")

        # 2. Deliver ONLY the currently authorized prompt to the inner agent.
        combined = f"{workspace_notice}\n\n{prompt}"
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tf:
            tf.write(combined)
            prompt_local = Path(tf.name)
        try:
            await self._env.upload_file(str(prompt_local), "/tmp/.agencybench_prompt.txt")
        finally:
            prompt_local.unlink(missing_ok=True)

        cmd = self._build_inner_command()
        exec_res = await self._env.exec(
            cmd,
            cwd="/workspace",
            env=self._extra_env or None,
        )

        # 3. Clean temporary prompt and pull fresh /workspace back to trusted host dir.
        # Clear host workspace first so files deleted/moved in-container do not linger.
        await self._env.exec("rm -f /tmp/.agencybench_prompt.txt")
        clear_directory(workspace)
        await self._env.download_dir("/workspace", str(workspace))
        return (exec_res.stdout or "") + ("\n" + exec_res.stderr if exec_res.stderr else "")

    def _build_inner_command(self) -> str:
        model = shlex.quote(self._model_name or "")
        if self._inner_agent == "fixture":
            return self._fixture_cmd or "true"
        if self._inner_agent == "claude-code":
            return f'claude -p "$(cat /tmp/.agencybench_prompt.txt)" --model {model} --dangerously-skip-permissions'
        if self._inner_agent == "codex":
            return f'codex exec --model {model} --full-auto "$(cat /tmp/.agencybench_prompt.txt)"'
        if self._inner_agent == "sii":
            return (
                "python3 -c "
                + shlex.quote(
                    "import asyncio, os; from pathlib import Path; "
                    "from sii_agent_sdk import SiiAgentOptions, SiiAgentSession; "
                    "prompt = Path('/tmp/.agencybench_prompt.txt').read_text(); "
                    "opts = SiiAgentOptions(cwd='/workspace', yolo=True, "
                    "model=os.environ.get('SII_TARGET_MODEL', ''), "
                    "auth_type=os.environ.get('SII_AUTH_TYPE', 'USE_OPENAI'), "
                    "max_turns=int(os.environ.get('SII_MAX_TURNS', '1000')), "
                    "enable_data_upload=False); "
                    "async def main():\n"
                    "  s = SiiAgentSession(opts)\n"
                    "  async for m in s.run(prompt, options=opts): print(m)\n"
                    "asyncio.run(main())"
                )
            )
        raise ValueError(f"Unsupported inner_agent: {self._inner_agent!r}")


class AgencyBenchAgent(BaseAgent):
    """Harbor BaseAgent running AgencyBench's trusted multi-stage scenario loop."""

    def __init__(
        self,
        *args: Any,
        upstream_root: str | None = None,
        inner_agent: str = "sii",
        max_attempts: int | None = None,
        fixture_cmd: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._upstream_root = Path(
            upstream_root or os.environ.get("AGENCYBENCH_UPSTREAM_ROOT", "")
        )
        self._inner_agent = inner_agent
        self._max_attempts = max_attempts
        self._fixture_cmd = fixture_cmd

    @staticmethod
    def name() -> str:
        return "agencybench-controller"

    def version(self) -> str | None:
        return "0.1.0"

    async def setup(self, environment: BaseEnvironment) -> None:
        return

    def _collect_forwarded_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        for key in FORWARDED_ENV_VARS:
            val = os.environ.get(key)
            if val is not None:
                env[key] = val
        base_extra = getattr(self, "_extra_env", None)
        if isinstance(base_extra, dict):
            env.update(base_extra)
        return env

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        # Resolve scenario task_id from task metadata marker in instruction header.
        task_id = self._extract_task_id(instruction)
        spec = SCENARIO_BY_TASK_ID[task_id]
        upstream_dir = self._upstream_root / spec.upstream_rel_path
        if not upstream_dir.is_dir():
            raise FileNotFoundError(
                f"AgencyBench upstream scenario dir missing: {upstream_dir}. "
                "Set AGENCYBENCH_UPSTREAM_ROOT or --agent-kwarg upstream_root=..."
            )

        run_root = Path(self.logs_dir) / "trusted_controller"
        controller = ScenarioController(
            spec=spec,
            upstream_scenario_dir=upstream_dir,
            run_root=run_root,
            max_attempts=self._max_attempts,
        )
        loop = asyncio.get_running_loop()
        transport: AgentTransport = HarborContainerTransport(
            loop=loop,
            environment=environment,
            inner_agent=self._inner_agent,
            model_name=self.model_name,
            extra_env=self._collect_forwarded_env(),
            fixture_cmd=self._fixture_cmd,
        )
        # Execute staged controller in worker thread so loop.run_until_complete is clean.
        result = await asyncio.to_thread(controller.run, transport)

        # After inner agent completes, upload winning stage snapshots for independent verifier replay.
        await environment.exec("rm -rf /tmp/.agencybench_stages && mkdir -p /tmp/.agencybench_stages")
        await environment.upload_dir(str(result.stages_dir), "/tmp/.agencybench_stages")

        if hasattr(context, "metadata"):
            if context.metadata is None:
                context.metadata = {}
            if isinstance(context.metadata, dict):
                context.metadata["agencybench_controller"] = {
                    "scenario_id": result.scenario_id,
                    "task_id": result.task_id,
                    "reward": result.reward,
                    "s_avg_pct": result.s_avg_pct,
                    "pass_at_1": result.pass_at_1,
                    "pass_at_2": result.pass_at_2,
                    "avg_attempts": result.avg_attempts,
                }

    @staticmethod
    def _extract_task_id(instruction: str) -> str:
        for line in instruction.splitlines()[:20]:
            stripped = line.strip()
            if stripped.startswith("<!-- agencybench_task_id:"):
                return stripped.split(":", 1)[1].replace("-->", "").strip()
        raise ValueError("Missing agencybench_task_id marker in instruction.md")
