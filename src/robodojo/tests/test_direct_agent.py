"""Whole-lifecycle checks with a fake model API and a fake native simulator.

The Harbor agent, the bridge client, the HTTP broker, the native policy model,
the simulator-side episode loop and the verifier are the real implementations.
Only the vision model, the Isaac Sim environment and Docker are replaced.
"""

import asyncio
import importlib.util
import json
from pathlib import Path
import shlex
import shutil
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

from harbor.models.agent.context import AgentContext

from robodojo.catalog import XPOLICYLAB_REVISION
from robodojo.direct import bridge_client
from robodojo.direct.broker import Broker, make_server
from robodojo.direct.episode import DirectAbort, run_episode
from robodojo.direct.prompts import PROMPT_SHA256, SYSTEM_PROMPT, build_messages
from robodojo.direct_agent import BRIDGE_COMMAND, DirectAgent, DirectRunError
from robodojo.runtime import EvaluationError, write_json
from robodojo.verifier import verify

from helpers import FakeEnv, FakeKinematics, hold, model_call, task_spec_dict

POLICY = Path(__file__).parents[1] / "src" / "robodojo" / "direct" / "policy"


class FakeEnvironment:
    """Stand-in for the agent container: runs the real bridge client in-process."""

    def __init__(self, url, out):
        self.url, self.out, self.commands = url, out, []

    async def exec(self, command, timeout_sec=None, **kwargs):
        self.commands.append(command)
        assert command.startswith(BRIDGE_COMMAND + " ")
        arguments = shlex.split(command[len(BRIDGE_COMMAND):])
        lines = []
        code = await asyncio.to_thread(
            bridge_client.main, ["--url", self.url, "--out", str(self.out), *arguments], lines.append,
        )
        return SimpleNamespace(return_code=code, stdout="\n".join(lines), stderr="")

    async def download_dir(self, source_dir, target_dir):
        shutil.copytree(source_dir, target_dir, dirs_exist_ok=True)


def tool_response(arguments, call_id="call_1", tokens=(100, 20)):
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments)
    return {
        "choices": [{"message": {"content": None, "tool_calls": [
            {"id": call_id, "function": {"name": "robodojo_act", "arguments": arguments}},
        ]}}],
        "usage": {"prompt_tokens": tokens[0], "completion_tokens": tokens[1],
                  "prompt_tokens_details": {"cached_tokens": 10}},
    }


def current_observation(messages):
    """The model's view: the text of the newest observation turn."""
    for message in reversed(messages):
        if message["role"] == "user" and isinstance(message["content"], list):
            return json.loads(message["content"][0]["text"])
    raise AssertionError("No observation in the prompt")


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.results = self.directory / "results"
        self.broker = Broker(self.results, poll_interval=0.01)
        self.server = make_server(self.broker, "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        spec = importlib.util.spec_from_file_location("harbor_direct_model_agent", POLICY / "model.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.call = model_call(module.Model({}, broker=self.broker, serve=False))
        self.environment = FakeEnvironment(
            f"http://127.0.0.1:{self.server.server_address[1]}", self.directory / "container-out",
        )
        self.spec_path = self.directory / "task_spec.json"
        self.spec_path.write_text(json.dumps(task_spec_dict()))
        self.simulator_error = None
        self.prompts = []

    def simulator(self, episodes=50, step_lim=7, succeed=lambda layout: layout % 5 == 0, on_done=None):
        """Mimic the native loop: one episode per layout, then the native result."""
        def loop():
            details, successes, total = {}, 0, 0.0
            try:
                for layout in range(episodes):
                    env = FakeEnv(layout_id=layout, step_lim=step_lim,
                                  succeed_at=3 if succeed(layout) else None)
                    run_episode(env, self.call, kinematics_factory=FakeKinematics,
                                sleep=lambda seconds: time.sleep(0.001))
                    score = 1.0 if env.success[0] else 0.5
                    details[str(layout)] = {"layout_id": layout, "success": env.success[0], "score": score}
                    successes += env.success[0]
                    total += score
                native = self.results / "native" / "insert_tubes" / "_result.json"
                native.parent.mkdir(parents=True)
                write_json(native, {
                    "eval_time": episodes, "success_rate": successes / episodes,
                    "score": total / episodes * 100, "details": details,
                })
                verdict = {
                    "status": "completed", **{key: task_spec_dict()[key] for key in (
                        "task_id", "dimension", "seed", "upstream_revision")},
                    "xpolicylab_revision": XPOLICYLAB_REVISION, "policy_name": "HarborDirect",
                    "variants": [{"name": "insert_tubes", "episodes": episodes, "run_id": "harbor_test"}],
                }
            except (Exception, DirectAbort) as error:
                self.simulator_error = error
                verdict = {"status": "failed", "error": str(error)}
            write_json(self.results / "supervisor.json", verdict)
        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 20)
        return thread

    def agent(self, completion, **options):
        async def completion_fn(**kwargs):
            self.prompts.append(kwargs)
            return completion(kwargs["messages"])
        agent = DirectAgent(
            logs_dir=self.directory / "agent-logs", model_name="fake/vision-model",
            completion_fn=completion_fn, cost_fn=options.pop("cost_fn", lambda response: 0.001),
            wait_sec=20, **options,
        )
        async def no_sleep(seconds):
            return None
        agent._sleep = no_sleep
        return agent

    def run_agent(self, agent):
        context = AgentContext()

        async def main():
            await agent.setup(self.environment)
            await agent.run("unused instruction", self.environment, context)

        try:
            asyncio.run(main())
        finally:
            self.context = context
        return context

    def test_complete_task_yields_the_native_reward(self):
        thread = self.simulator()
        invalid_once = []

        def completion(messages):
            observation = current_observation(messages)
            decision = hold(observation)
            if not invalid_once:
                # The first answer moves too far; the scaffold must let the model correct it.
                invalid_once.append(observation["request_id"])
                decision["target"]["left"]["position"][2] += 0.5
            return tool_response(decision)

        context = self.run_agent(self.agent(completion))
        thread.join(20)
        self.assertIsNone(self.simulator_error)
        reward = verify(self.spec_path, self.results, self.directory / "verifier")
        # Ten of fifty layouts succeed; the others keep the native partial score of 0.5.
        self.assertEqual(reward, {"success_rate": 0.2, "score": 0.6})
        evaluation = json.loads((self.directory / "verifier" / "evaluation.json").read_text())
        self.assertEqual(evaluation["variants"][0]["layout_ids"], list(range(50)))
        # Failing episodes need two decisions for seven steps, succeeding ones need one.
        self.assertEqual(evaluation["variants"][0]["decisions"], 40 * 2 + 10)
        self.assertEqual(evaluation["variants"][0]["control_steps"], 40 * 7 + 10 * 3)
        direct = context.metadata["robodojo_direct"]
        self.assertEqual(direct["stats"]["episodes"], 50)
        self.assertEqual(direct["stats"]["decisions"], 90)
        self.assertEqual(direct["stats"]["invalid_responses"], 1)
        self.assertEqual(direct["stats"]["model_calls"], 91)
        self.assertEqual(direct["prompt_sha256"], PROMPT_SHA256)
        self.assertEqual(direct["model"], "fake/vision-model")
        self.assertEqual(context.n_input_tokens, 91 * 100)
        self.assertEqual(context.n_output_tokens, 91 * 20)
        self.assertEqual(context.n_cache_tokens, 91 * 10)
        self.assertAlmostEqual(context.cost_usd, 0.091)
        self.assertTrue((self.directory / "agent-logs" / "robodojo_direct" / "run.json").is_file())

    def test_prompt_carries_native_instruction_frames_and_fresh_history_per_episode(self):
        thread = self.simulator(episodes=50, step_lim=7, succeed=lambda layout: False)
        self.run_agent(self.agent(lambda messages: tool_response(hold(current_observation(messages)))))
        thread.join(20)
        first, second, third = (self.prompts[index] for index in range(3))
        self.assertEqual(first["model"], "fake/vision-model")
        self.assertEqual(first["tool_choice"], "required")
        self.assertEqual(first["tools"][0]["function"]["name"], "robodojo_act")
        self.assertEqual(first["messages"][0], {"role": "system", "content": SYSTEM_PROMPT})
        observation = first["messages"][1]["content"]
        self.assertEqual(json.loads(observation[0]["text"])["instruction"], "Insert the tubes into the rack.")
        images = [part for part in observation if part["type"] == "image_url"]
        self.assertEqual(len(images), 3)
        self.assertTrue(images[0]["image_url"]["url"].startswith("data:image/png;base64,iVBOR"))
        # Second decision of the episode: the earlier turn is kept without frames.
        roles = [message["role"] for message in second["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "tool", "user"])
        self.assertEqual(sum(part["type"] == "image_url" for part in second["messages"][1]["content"]), 0)
        self.assertEqual(sum(part["type"] == "image_url" for part in second["messages"][4]["content"]), 3)
        self.assertEqual(
            json.loads(second["messages"][4]["content"][0]["text"])["previous_result"]["executed_steps"], 5,
        )
        # The next episode starts with an empty history.
        self.assertEqual([message["role"] for message in third["messages"]], ["system", "user"])
        # No credentials or model settings ever enter the agent container.
        self.assertTrue(all(command.startswith(BRIDGE_COMMAND) for command in self.environment.commands))

    def test_repeated_invalid_answers_fall_back_to_a_recorded_hold(self):
        thread = self.simulator(succeed=lambda layout: False)
        answers = iter(["not json", {"mode": "eef"}, None])

        def completion(messages):
            answer = next(answers, "valid")
            if answer == "valid":
                return tool_response(hold(current_observation(messages)))
            if answer is None:
                return {"choices": [{"message": {"content": "I will wait.", "tool_calls": None}}], "usage": {}}
            return tool_response(answer)

        context = self.run_agent(self.agent(completion, max_invalid_responses=3))
        thread.join(20)
        stats = context.metadata["robodojo_direct"]["stats"]
        self.assertEqual((stats["invalid_responses"], stats["fallback_holds"]), (3, 1))
        accepted = [json.loads(line) for line in
                    (self.results / "broker" / "decisions" / "0000.jsonl").read_text().splitlines()]
        self.assertEqual(accepted[0]["response"]["steps"], 1)
        self.assertIn("Scaffold hold", accepted[0]["response"]["reason"])
        verify(self.spec_path, self.results, self.directory / "verifier")

    def test_spending_limit_aborts_and_leaves_no_reward(self):
        self.simulator(succeed=lambda layout: False)
        agent = self.agent(
            lambda messages: tool_response(hold(current_observation(messages))),
            max_cost_usd=0.0025, cost_fn=lambda response: 0.001,
        )
        with self.assertRaisesRegex(DirectRunError, "spending limit"):
            self.run_agent(agent)
        self.assertTrue((self.results / "broker" / "abort.json").is_file())
        self.assertEqual(self.context.metadata["robodojo_direct"]["stats"]["model_calls"], 3)
        with self.assertRaisesRegex(EvaluationError, "aborted"):
            verify(self.spec_path, self.results, self.directory / "verifier")
        self.assertFalse((self.directory / "verifier" / "reward.json").exists())

    def test_unknown_price_cannot_bypass_a_spending_limit(self):
        self.simulator(succeed=lambda layout: False)
        agent = self.agent(
            lambda messages: tool_response(hold(current_observation(messages))),
            max_cost_usd=5.0, cost_fn=lambda response: None,
        )
        with self.assertRaisesRegex(DirectRunError, "price of this model is unknown"):
            self.run_agent(agent)
        self.assertIsNone(self.context.cost_usd)

    def test_decision_limit_aborts_the_run(self):
        self.simulator(step_lim=30, succeed=lambda layout: False)
        agent = self.agent(
            lambda messages: tool_response(hold(current_observation(messages), steps=1)),
            max_decisions_per_episode=4,
        )
        with self.assertRaisesRegex(DirectRunError, "decision limit"):
            self.run_agent(agent)
        with self.assertRaises(EvaluationError):
            verify(self.spec_path, self.results, self.directory / "verifier")

    def test_model_api_errors_are_retried_then_fatal(self):
        self.simulator(succeed=lambda layout: False)
        failures = []

        def completion(messages):
            failures.append(1)
            raise TimeoutError("provider overloaded")

        with self.assertRaisesRegex(DirectRunError, "Model API failed after 3 attempts"):
            self.run_agent(self.agent(completion, api_retries=2))
        self.assertEqual(len(failures), 3)
        # Nothing was executed while the model was unavailable.
        self.assertFalse((self.results / "broker" / "decisions").exists())

    def test_failed_simulator_run_fails_the_trial(self):
        write_json(self.results / "supervisor.json", {"status": "failed", "error": "simulator crashed"})
        with self.assertRaisesRegex(DirectRunError, "simulator crashed"):
            self.run_agent(self.agent(lambda messages: self.fail("The model must not be called")))


class AgentConfigurationTests(unittest.TestCase):
    def test_model_is_required_and_options_are_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "vision model"):
                DirectAgent(logs_dir=Path(directory))
            for options in (
                {"max_invalid_responses": 0}, {"image_history": 0}, {"history_window": -1},
                {"max_cost_usd": 0}, {"tool_choice": "none"}, {"wait_sec": 0}, {"api_retries": True},
            ):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    DirectAgent(logs_dir=Path(directory), model_name="fake/model", **options)
            agent = DirectAgent(logs_dir=Path(directory), model_name="fake/model", temperature=0.0)
            self.assertEqual(agent.name(), "robodojo-direct")
            self.assertEqual(agent.provenance()["settings"]["temperature"], 0.0)

    def test_history_window_drops_old_turns_and_keeps_tool_pairs(self):
        turns = []
        for index in range(4):
            turns.append({"kind": "observation", "text": f"observation {index}", "images": {"cam_head": b"png"}})
            turns.append({"kind": "attempt", "call_id": f"call_{index}", "arguments": "{}", "result": "{}"})
        messages = build_messages(turns[:-1], image_history=2, history_window=2)
        self.assertEqual(
            [message["role"] for message in messages],
            ["system", "user", "user", "assistant", "tool", "user"],
        )
        self.assertIn("dropped", messages[1]["content"])
        self.assertEqual(messages[2]["content"][0]["text"], "observation 2")
        self.assertEqual(sum(part["type"] == "image_url" for part in messages[2]["content"]), 1)
        full = build_messages(turns, image_history=1, history_window=0)
        self.assertEqual(len(full), 1 + 4 * 3)
        self.assertEqual(sum(
            part["type"] == "image_url" for message in full if isinstance(message["content"], list)
            for part in message["content"]
        ), 1)


if __name__ == "__main__":
    unittest.main()
