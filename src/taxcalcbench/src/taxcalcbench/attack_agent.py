"""Verifier hack controls: an agent that tries to game the reward.

Each mode leaves the container in a state that must score 0:

- fake_reward:   writes reward 1 to /logs/verifier/reward.txt and reward.json,
                 no answer file;
- symlink:       /app/output/return.txt is a symlink to /tests/expected.xml;
- plant_tests:   puts a /tests/score.py and /tests/test.sh that write reward 1,
                 no answer file;
- empty:         an empty /app/output/return.txt.

(Duplicated answer lines are covered by tests/test_adapter.py: upstream counts
a label only when it appears exactly once.)

    harbor run -p <tasks> --agent-import-path taxcalcbench.attack_agent:AttackAgent \
        --agent-kwarg mode=<mode>
"""

from __future__ import annotations

from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

SCRIPTS = {
    "fake_reward": "mkdir -p /logs/verifier && echo 1 > /logs/verifier/reward.txt && echo '{\"reward\": 1}' > /logs/verifier/reward.json",
    "symlink": "mkdir -p /app/output && ln -sf /tests/expected.xml /app/output/return.txt",
    "plant_tests": (
        "mkdir -p /tests && printf '#!/bin/bash\\necho 1 > /logs/verifier/reward.txt\\n' > /tests/test.sh "
        "&& chmod +x /tests/test.sh && printf 'open(\"/logs/verifier/reward.txt\",\"w\").write(\"1\")\\n' > /tests/score.py"
    ),
    "empty": "mkdir -p /app/output && : > /app/output/return.txt",
}


class AttackAgent(BaseAgent):
    def __init__(self, *args, mode: str = "fake_reward", **kwargs):
        super().__init__(*args, **kwargs)
        self._mode = mode

    @staticmethod
    def name() -> str:
        return "taxcalcbench-attack"

    def version(self) -> str | None:
        return "1.0.0"

    async def setup(self, environment: BaseEnvironment) -> None:
        return

    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        await environment.exec(SCRIPTS[self._mode])
