"""Check that the Harbor agent submits only a validated external policy endpoint."""

import json
from pathlib import Path
import shlex
import tempfile
from types import SimpleNamespace
import unittest

from harbor.models.agent.context import AgentContext

from robodojo.policy_agent import EndpointPolicyAgent
from robodojo.runtime import EvaluationError


class StubEnvironment:
    def __init__(self, return_code=0):
        self.return_code = return_code
        self.calls = []

    async def exec(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(return_code=self.return_code)


class PolicyAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.logs_dir = Path(self.temporary.name)

    def make_agent(self, **options):
        arguments = {"logs_dir": self.logs_dir, "policy_name": "GR00T_N17",
                     "host": "policy.example.org", "port": 9999}
        arguments.update(options)
        return EndpointPolicyAgent(**arguments)

    async def test_agent_writes_one_shell_quoted_endpoint_submission(self):
        agent = self.make_agent()
        environment = StubEnvironment()
        context = AgentContext()
        await agent.setup(environment)
        self.assertEqual(environment.calls, [])
        await agent.run("Native observations go to the policy server", environment, context)
        self.assertEqual(len(environment.calls), 1)
        call = environment.calls[0]
        self.assertEqual(call["timeout_sec"], 30)
        words = shlex.split(call["command"])
        self.assertEqual(words[:2], ["printf", "%s\\n"])
        self.assertEqual(words[3:], [">", "/workspace/submission.json"])
        endpoint = {"policy_name": "GR00T_N17", "host": "policy.example.org", "port": 9999}
        self.assertEqual(json.loads(words[2]), endpoint)
        self.assertEqual(context.metadata, {"external_policy": endpoint})

    async def test_nonzero_submission_write_fails_the_agent(self):
        environment = StubEnvironment(return_code=1)
        with self.assertRaisesRegex(RuntimeError, "Could not write"):
            await self.make_agent().run("", environment, AgentContext())

    def test_harbor_model_name_is_rejected_for_external_policy_selection(self):
        with self.assertRaisesRegex(ValueError, "model_name"):
            self.make_agent(model_name="openai/example-model")

    def test_unsafe_endpoint_fields_are_rejected_before_execution(self):
        for options in (
            {"host": "policy.example.org; touch /tmp/injected"},
            {"host": "$(id)"}, {"host": "policy.example.org\nnext"},
            {"policy_name": "../evil"}, {"port": "9999"}, {"port": True},
        ):
            with self.subTest(options=options):
                with self.assertRaises(EvaluationError):
                    self.make_agent(**options)


if __name__ == "__main__":
    unittest.main()
