"""Harbor Daytona backend extension scoped to this trial's trusted broker."""
import asyncio
import contextlib
import json

from harbor.environments.daytona.environment import DaytonaEnvironment
from .bridge import Bridge


class MirrorDaytonaEnvironment(DaytonaEnvironment):
    async def start(self, force_build: bool) -> None:
        self._mirror_task = None
        allowed = self.environment_dir / "broker/allowed-tools.json"
        if allowed.is_file():
            self._persistent_env["STB_MIRROR_URL"] = "http://127.0.0.1:18180/virtual"
        await super().start(force_build)
        if not allowed.is_file():
            return  # Separate verifier environment has no tool broker.
        try:
            async def in_broker(command):
                result = await self.service_exec(command, service="broker", timeout_sec=40)
                if result.return_code != 0:
                    raise RuntimeError(f"Trusted broker command failed ({result.return_code}): {result.stderr}")
                return result.stdout or ""
            bridge = Bridge(in_broker, json.loads(allowed.read_text()),
                            self.trial_paths.trial_dir / "mirror-relay.jsonl")
            await bridge.start()
            self._mirror_task = asyncio.create_task(bridge.run())
        except BaseException:
            await super().stop(delete=True)
            raise

    async def stop(self, delete: bool):
        try:
            task = getattr(self, "_mirror_task", None)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        finally:
            await super().stop(delete)
