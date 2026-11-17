"""Harbor agent that plays RoboDojo in Direct mode with a vision model.

The agent runs on the Harbor host. The decision loop itself is
``direct.player.DirectPlayer``, which the original-side driver runs as well;
this class only connects it to Harbor: the bridge client is executed in the
agent container, and the token counts, the cost and the provenance are handed
to Harbor's trial record. Credentials never enter a container.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
import shlex
from typing import Any

from harbor.agents.base import BaseAgent

from .direct.player import (
    AGENT_NAME, AGENT_VERSION, PLAYER_OPTIONS, DirectPlayer, DirectRunError, public_endpoint,
)

BRIDGE_COMMAND = "python3 /opt/robodojo/bridge_client.py"

__all__ = ["AGENT_VERSION", "BRIDGE_COMMAND", "DirectAgent", "DirectRunError", "public_endpoint"]


class HarborBridge:
    """Runs the bridge client in the agent container of a Harbor trial."""

    def __init__(self, environment: Any) -> None:
        self.environment = environment

    async def run(self, arguments: Sequence[str], timeout: float) -> tuple[int, str, str]:
        command = BRIDGE_COMMAND + " " + " ".join(shlex.quote(argument) for argument in arguments)
        result = await self.environment.exec(command=command, timeout_sec=int(timeout))
        return result.return_code, result.stdout or "", result.stderr or ""

    async def download(self, source_dir: str, target_dir: Path) -> None:
        await self.environment.download_dir(source_dir, target_dir)


class DirectAgent(BaseAgent):
    """``DirectPlayer`` as a Harbor agent. Its options are the player's options."""

    DEFAULT_SETUP_TIMEOUT_SEC = 1800.0

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # The player defines the options and their defaults; Harbor keeps the rest.
        options = {name: kwargs.pop(name) for name in PLAYER_OPTIONS if name in kwargs}
        super().__init__(*args, **kwargs)
        if not self.model_name:
            raise ValueError("DirectAgent needs a vision model, for example -m provider/model-name")
        self.player = DirectPlayer(
            self.model_name, Path(self.logs_dir) / "robodojo_direct",
            get_env=self._get_env, logger=self.logger, **options,
        )

    @staticmethod
    def name() -> str:
        return AGENT_NAME

    def version(self) -> str:
        return AGENT_VERSION

    def provenance(self) -> dict[str, Any]:
        return self.player.provenance()

    async def setup(self, environment) -> None:
        """Wait until the broker in the simulator container answers."""
        await self.player.setup(HarborBridge(environment), self.DEFAULT_SETUP_TIMEOUT_SEC - 60)

    async def run(self, instruction, environment, context) -> None:
        try:
            await self.player.run(HarborBridge(environment))
        finally:
            stats = self.player.stats
            context.n_input_tokens = stats["input_tokens"]
            context.n_cache_tokens = stats["cached_tokens"]
            context.n_output_tokens = stats["output_tokens"]
            context.cost_usd = stats["cost_usd"] if stats["cost_known"] else None
            context.metadata = {"robodojo_direct": self.player.summary()}
