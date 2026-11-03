"""Harbor agents that keep working until the budget ends, as upstream's wrappers do.

Upstream's agents/common.sh reruns the CLI with a "continue" prompt whenever it exits before
the wall-clock budget ends, and src/run_task.sh restarts a crashed agent up to three times.
Harbor's stock agents run once, so stock runs and upstream runs differ in how much of the
budget they use. Parity runs use these agents.
"""

import shlex
import time
from typing import override

from harbor.agents.installed.base import NonZeroAgentExitCodeError
from harbor.agents.installed.claude_code import ClaudeCode
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

MAX_RUNS = 3
MIN_RESTART_SECONDS = 60


def continue_prompt(seconds_left: int) -> str:
    """Return the prompt from upstream's agents/common.sh."""
    return (
        "Continue where you left off and complete the task. You have approximately "
        f"{seconds_left // 60} minutes ({seconds_left} seconds) of wall-clock time remaining. "
        "Make sure you utilize this time fully to achieve the best results you can. "
        "Do not ask for user feedback."
    )


class ClaudeCodeUntilDeadline(ClaudeCode):
    """Claude Code with upstream's continuation loop."""

    async def _seconds_left(self, environment: BaseEnvironment) -> int:
        result = await self.exec_as_agent(
            environment,
            command=(
                "echo $(( $(cat /timer_start) + "
                "$(awk -v h=\"$NUM_HOURS\" 'BEGIN{printf \"%d\", h*3600}') - $(date +%s) ))"
            ),
        )
        return int(result.stdout.strip())

    async def _keep_transcript(self, environment: BaseEnvironment, phase: int) -> None:
        transcript = (self.environment_logs_dir / "claude-code.txt").as_posix()
        kept = (self.environment_logs_dir / f"claude-code.{phase}.txt").as_posix()
        await self.exec_as_agent(
            environment,
            command=f"cp -f {shlex.quote(transcript)} {shlex.quote(kept)} || true",
        )

    @override
    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        deadline = time.monotonic() + await self._seconds_left(environment)
        prompt = instruction
        crashes = phase = 0
        while True:
            try:
                await super().run(prompt, environment, context)
            except NonZeroAgentExitCodeError:
                crashes += 1
                if crashes >= MAX_RUNS or deadline - time.monotonic() < MIN_RESTART_SECONDS:
                    raise
            await self._keep_transcript(environment, phase)
            phase += 1
            left = int(deadline - time.monotonic())
            if left <= 0:
                return
            prompt = continue_prompt(left)
            self._resume = True
