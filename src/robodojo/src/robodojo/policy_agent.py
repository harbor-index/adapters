"""Harbor agent that selects an already-running native XPolicyLab policy.

The external policy receives the observations and produces robot actions. This
agent only submits its endpoint; it does not claim to implement a robot policy.
"""

from __future__ import annotations

import json
from pathlib import Path
import shlex
import tempfile

from harbor.agents.base import BaseAgent

from .runtime import load_submission


class EndpointPolicyAgent(BaseAgent):
    def __init__(self, *args, policy_name: str, host: str, port: int, **kwargs):
        if kwargs.get("model_name") is not None:
            raise ValueError("Configure the policy on its server; do not pass a Harbor model_name")
        super().__init__(*args, **kwargs)
        self.submission = {"policy_name": policy_name, "host": host, "port": port}
        # Share exactly the verifier's endpoint validation before any trial runs.
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "submission.json"
            path.write_text(json.dumps(self.submission))
            load_submission(path)

    @staticmethod
    def name() -> str:
        return "robodojo-endpoint"

    def version(self) -> str:
        return "0.1.0"

    async def setup(self, environment) -> None:
        return

    async def run(self, instruction, environment, context) -> None:
        payload = shlex.quote(json.dumps(self.submission))
        result = await environment.exec(
            command=f"printf '%s\\n' {payload} > /workspace/submission.json",
            timeout_sec=30,
        )
        if result.return_code != 0:
            raise RuntimeError("Could not write the policy endpoint submission")
        context.metadata = {"external_policy": self.submission}
