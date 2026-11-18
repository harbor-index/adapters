"""Checks of the CPU mock: its pictures, its end-to-end run, and its isolation.

The mock replaces the simulator with an invented scene. These tests show that
the whole chain runs without a GPU and that a mock result can never be taken
for a RoboDojo result.
"""

import asyncio
import base64
import contextlib
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from unittest import mock

from harbor.models.agent.context import AgentContext

from robodojo.adapter import MOCK_HARBOR_NAME, RoboDojoAdapter
from robodojo.aggregate import aggregate
from robodojo.catalog import MOCK_TASK_ID, mock_task_spec
from robodojo.direct import supervisor
from robodojo.direct.broker import encode_png
from robodojo.direct.contract import STEP_TRANSLATION_M
from robodojo.direct.episode import joint_action
from robodojo.direct_agent import DirectAgent, DirectRunError, HarborBridge
from robodojo.mock import fake_openai, local, scene, service, solver
from robodojo.runtime import EvaluationError, load_spec
from robodojo.verifier import verify

from helpers import task_spec_dict


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def head_png(env):
    frame = env.get_obs()["vision"]["cam_head"]["color"]
    return encode_png(frame.shape[1], frame.shape[0], frame.tobytes())


class SceneTests(unittest.TestCase):
    def test_pictures_have_the_native_shape_and_show_the_layout(self):
        env = scene.MockEnv(MOCK_TASK_ID, layout_id=3)
        observation = env.get_obs()
        self.assertEqual(tuple(observation["vision"]), scene.CAMERAS)
        self.assertEqual(observation["vision"]["cam_head"]["color"].shape, (200, 320, 3))
        for camera in scene.CAMERAS:
            frame = observation["vision"][camera]["color"]
            self.assertEqual((frame.dtype.name, frame.ndim), ("uint8", 3))
        self.assertIn("not a RoboDojo task", observation["instruction"])
        # Same seed and layout draw the same scene; another layout differs.
        again = scene.MockEnv(MOCK_TASK_ID, layout_id=3)
        self.assertEqual(head_png(env), head_png(again))
        self.assertNotEqual(head_png(env), head_png(scene.MockEnv(MOCK_TASK_ID, layout_id=4)))
        self.assertNotEqual(head_png(env), head_png(scene.MockEnv(MOCK_TASK_ID, layout_id=3, eval_seed=1)))

    def test_solver_reads_the_disc_positions_from_the_picture(self):
        self.assertEqual(
            (solver.HEAD_X_MAX, solver.HEAD_Y_MAX, solver.HEAD_PIXELS_PER_METER, solver.TARGET_COLORS),
            (scene.HEAD_X[1], scene.HEAD_Y[1], scene.HEAD_PIXELS_PER_METER, scene.TARGET_COLORS),
        )
        self.assertLess(solver.GOAL_HEIGHT_M, scene.REACH_HEIGHT_M)
        for layout in range(20):
            env = scene.MockEnv(MOCK_TASK_ID, layout_id=layout)
            found = solver.locate(head_png(env))
            for arm in ("left", "right"):
                with self.subTest(layout=layout, arm=arm):
                    error = max(abs(found[arm][axis] - env.targets[arm][axis]) for axis in (0, 1))
                    self.assertLess(error, 0.005)

    def test_motion_is_bounded_per_step_and_success_needs_both_arms(self):
        env = scene.MockEnv(MOCK_TASK_ID, layout_id=0, step_lim=30)
        kinematics = scene.MockKinematics(env)

        def command(closed_right):
            # Ask for the goal directly; the mock arm may only move one step toward it.
            return {
                arm: {
                    "position": [*env.targets[arm], 0.10], "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0],
                    "gripper_closed": arm == "left" or closed_right,
                }
                for arm in ("left", "right")
            }

        before = env.joints["left"][:3].copy()
        env.take_action(joint_action(kinematics.target(command(False))["action"]))
        moved = float(((env.joints["left"][:3] - before) ** 2).sum() ** 0.5)
        self.assertAlmostEqual(moved, STEP_TRANSLATION_M, places=6)
        while not env.is_episode_end():
            env.take_action(joint_action(kinematics.target(command(False))["action"]))
        # The right gripper never closed: the step limit ends the episode as a failure.
        self.assertEqual((env.take_action_cnt[0], env.success[0], env.score()), (30, False, 0.5))
        solved = scene.MockEnv(MOCK_TASK_ID, layout_id=0)
        kinematics = scene.MockKinematics(solved)
        while not solved.is_episode_end():
            solved.take_action(joint_action(kinematics.target(command(True))["action"]))
        self.assertEqual((solved.success[0], solved.score()), (True, 1.0))
        self.assertLess(solved.take_action_cnt[0], 30)


class IsolationTests(unittest.TestCase):
    """A mock can only be loaded, run and graded as a mock."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def spec(self, value, name="spec.json"):
        path = self.directory / name
        path.write_text(json.dumps(value))
        return path

    def test_only_the_exact_mock_specification_is_accepted(self):
        spec = load_spec(self.spec(mock_task_spec(seed=4, episodes=3)))
        self.assertEqual((spec.mock, spec.task_id, spec.seed, spec.variants[0].episodes),
                         (True, MOCK_TASK_ID, 4, 3))
        self.assertFalse(load_spec(self.spec(task_spec_dict())).mock)
        valid = mock_task_spec()
        for change in (
            {"mock": False}, {"task_id": "insert_tubes"}, {"dimension": "precision"},
            {"upstream_revision": task_spec_dict()["upstream_revision"]},
            {"variants": [{"name": MOCK_TASK_ID, "episodes": 0}]},
            {"variants": [{"name": MOCK_TASK_ID, "episodes": 51}]},
            {"variants": [{"name": "insert_tubes", "episodes": 2}]}, {"extra": 1}, {"seed": -1},
        ):
            with self.subTest(change=change), self.assertRaises(EvaluationError):
                load_spec(self.spec({**valid, **change}))
        # A benchmark specification cannot be relabelled as a mock, or the reverse.
        with self.assertRaises(EvaluationError):
            load_spec(self.spec({**task_spec_dict(), "mock": True}))
        with self.assertRaises(ValueError):
            mock_task_spec(episodes=0)

    def test_native_supervisor_and_mock_service_refuse_each_others_tasks(self):
        with self.assertRaisesRegex(EvaluationError, "mock service"):
            supervisor.supervise(
                self.spec(mock_task_spec()), self.directory, self.directory / "native-results",
                self.directory / "policy",
            )
        with contextlib.redirect_stderr(io.StringIO()):
            verdict = service.evaluate(
                self.spec(task_spec_dict(), "real.json"), self.directory / "mock-results",
                host="127.0.0.1", port=free_port(),
            )
        self.assertEqual(verdict["status"], "failed")
        self.assertIn("only runs the mock task", verdict["error"])

    def run_mock(self, output):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(local.main(["--episodes", "1", "--output", str(output)]), 0)
        return output

    def test_mock_results_cannot_be_graded_or_aggregated_as_benchmark_results(self):
        output = self.run_mock(self.directory / "run")
        verdict_path = output / "results" / "supervisor.json"
        previous = verdict_path.read_bytes()
        with contextlib.redirect_stderr(io.StringIO()):
            repeat = service.evaluate(output / "task_spec.json", output / "results")
        self.assertEqual(repeat["status"], "failed")
        self.assertEqual(verdict_path.read_bytes(), previous)
        report = json.loads((output / "verifier" / "evaluation.json").read_text())
        self.assertEqual((report["mock"], report["policy_name"], report["upstream_revision"]),
                         (True, "HarborDirectMock", "mock"))
        self.assertEqual(report["rewards"], {"success_rate": 1.0, "score": 1.0})
        # Graded against a benchmark task, the same files yield no reward.
        with self.assertRaises(EvaluationError):
            verify(self.spec(task_spec_dict()), output / "results", self.directory / "as-real")
        self.assertFalse((self.directory / "as-real" / "reward.json").exists())
        # A verdict that claims to be a mock is refused for a benchmark task,
        # even if every other field were made to match.
        verdict = json.loads((output / "results" / "supervisor.json").read_text())
        real = task_spec_dict()
        verdict.update({key: real[key] for key in ("task_id", "dimension", "seed", "upstream_revision")})
        (output / "results" / "supervisor.json").write_text(json.dumps(verdict))
        with self.assertRaisesRegex(EvaluationError, "mock simulator cannot grade"):
            verify(self.spec(real), output / "results", self.directory / "as-real")
        # Aggregation names the reason instead of averaging the trial.
        trial = self.directory / "job" / "trial"
        (trial / "verifier").mkdir(parents=True)
        (trial / "result.json").write_text(json.dumps({"exception_info": None}))
        (trial / "verifier" / "evaluation.json").write_text(json.dumps(report))
        with self.assertRaisesRegex(EvaluationError, "Mock results are not benchmark results"):
            aggregate(self.directory / "job")


class GeneratedMockTaskTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.task = RoboDojoAdapter(self.directory / "tasks", mock=True, mock_episodes=2).run()[0]

    def test_mock_task_needs_no_gpu_assets_or_upstream_code(self):
        from harbor.models.task.config import TaskConfig

        config = TaskConfig.model_validate_toml((self.task / "task.toml").read_text())
        self.assertEqual(config.task.name, MOCK_HARBOR_NAME)
        self.assertFalse(MOCK_HARBOR_NAME.startswith("robodojo/"))
        self.assertEqual((config.environment.gpus, config.verifier.environment_mode.value), (0, "separate"))
        metadata = tomllib.loads((self.task / "task.toml").read_text())["metadata"]
        self.assertEqual((metadata["mock"], metadata["validation_status"]), (True, "mock-not-a-benchmark-task"))
        compose = json.loads((self.task / "environment/docker-compose.yaml").read_text())
        self.assertEqual(set(compose["services"]), {"simulator"})
        simulator = compose["services"]["simulator"]
        self.assertEqual(set(simulator), {"build", "cpus", "mem_limit"})
        files = {str(path.relative_to(self.task)) for path in self.task.rglob("*") if path.is_file()}
        # Neither the native supervisor nor the robot kinematics is shipped.
        self.assertFalse({name for name in files if "supervisor" in name or "kinematics" in name})
        self.assertIn("environment/simulator/robodojo/mock/service.py", files)
        self.assertIn("solution/solve.py", files)
        dockerfile = (self.task / "environment/simulator.Dockerfile").read_text()
        self.assertIn("FROM python:3.12-slim", dockerfile)
        self.assertNotIn("robodojo-harbor", dockerfile)
        for text in ((self.task / "instruction.md").read_text(), (self.task / "task.toml").read_text()):
            self.assertNotIn("$", text)
        self.assertIn("not a RoboDojo task", (self.task / "instruction.md").read_text())
        for options in ({"task_ids": ["insert_tubes"]}, {"limit": 1}, {"mock_episodes": 0}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                RoboDojoAdapter(self.directory / "other", mock=True, **options)
        with self.assertRaisesRegex(ValueError, "assets directory is required"):
            RoboDojoAdapter(self.directory / "other")

    def test_generated_files_run_as_three_separate_programs(self):
        """What the three containers would run, each from its own generated files only."""
        simulator = self.task / "environment" / "simulator"
        results, port = self.directory / "results", free_port()
        # Simulator container: needs NumPy, and must load the generated copy.
        environment = {"PYTHONPATH": str(simulator), "PATH": "/usr/bin:/bin"}
        origin = subprocess.run(
            [sys.executable, "-c", "import robodojo.mock.service as module; print(module.__file__)"],
            env=environment, capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertTrue(origin.startswith(str(simulator)), origin)
        process = subprocess.Popen(
            [sys.executable, "-m", "robodojo.mock.service", "--spec", str(simulator / "task_spec.json"),
             "--results-dir", str(results), "--host", "127.0.0.1", "--port", str(port)],
            env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        self.addCleanup(process.wait, 10)
        self.addCleanup(process.kill)
        self.addCleanup(process.stdout.close)
        # Agent container: the oracle and the bridge client, standard library only.
        # It starts at once, as in Compose, and must wait for the bridge itself.
        solved = subprocess.run(
            [sys.executable, "-I", str(self.task / "solution" / "solve.py"),
             "--bridge", str(self.task / "environment" / "bridge_client.py"),
             "--url", f"http://127.0.0.1:{port}", "--out", str(self.directory / "bridge-out")],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(solved.returncode, 0, solved.stdout + solved.stderr)
        self.assertIn("finished", solved.stdout)
        # The service stays up for artifact collection, like the real supervisor.
        self.assertIsNone(process.poll())
        # Verifier container: standard library only, from the generated tests directory.
        program = (
            "import sys; sys.path.insert(0, sys.argv[1]); from robodojo.verifier import main; "
            "raise SystemExit(main(['--spec', sys.argv[2], '--results-dir', sys.argv[3], '--output-dir', sys.argv[4]]))"
        )
        graded = subprocess.run(
            [sys.executable, "-I", "-c", program, str(self.task / "tests"),
             str(self.task / "tests" / "task_spec.json"), str(results), str(self.directory / "verifier")],
            capture_output=True, text=True,
        )
        self.assertEqual(graded.returncode, 0, graded.stderr)
        reward = json.loads((self.directory / "verifier" / "reward.json").read_text())
        self.assertEqual(reward, {"success_rate": 1.0, "score": 1.0})
        ledger = [json.loads(line) for line in (results / "broker" / "episodes.jsonl").read_text().splitlines()]
        self.assertEqual([record["ended_by"] for record in ledger], ["native_termination"] * 2)


class RealAgentOnMockTests(unittest.TestCase):
    def test_direct_agent_plays_the_mock_from_its_pictures(self):
        """The real agent against the mock service, with a scripted stand-in for the model."""
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            spec_path = directory / "task_spec.json"
            spec_path.write_text(json.dumps(mock_task_spec(episodes=2)))
            port = free_port()
            threading.Thread(
                target=service.evaluate, args=(spec_path, directory / "results"),
                kwargs={"host": "127.0.0.1", "port": port}, daemon=True,
            ).start()
            environment = local.LocalEnvironment(f"http://127.0.0.1:{port}", directory / "bridge")
            deadline = time.monotonic() + 30
            while environment.call("status").get("status") == "transport_error":
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.05)
            targets, pictures = {}, []

            async def completion(**request):
                # Like a vision model, it only has the prompt: text plus pictures.
                content = next(
                    message["content"] for message in reversed(request["messages"])
                    if message["role"] == "user" and isinstance(message["content"], list)
                )
                observation = json.loads(content[0]["text"])
                images = [part["image_url"]["url"] for part in content if part["type"] == "image_url"]
                pictures.append(len(images))
                if observation["step_id"] == 0:
                    head = base64.b64decode(images[0].split(",", 1)[1])
                    targets.clear()
                    targets.update(solver.locate(head))
                return {
                    "model": "scripted-stand-in",
                    "choices": [{"message": {"content": None, "tool_calls": [{
                        "id": "call_1", "function": {
                            "name": "robodojo_act", "arguments": json.dumps(solver.plan(observation, targets)),
                        },
                    }]}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }

            agent = DirectAgent(
                logs_dir=directory / "agent", model_name="fake/vision-model",
                completion_fn=completion, cost_fn=lambda response: 0.0, wait_sec=30,
            )
            context = AgentContext()

            async def play():
                await agent.setup(environment)
                await agent.run("unused", environment, context)

            asyncio.run(play())
            stats = context.metadata["robodojo_direct"]["stats"]
            self.assertEqual((stats["episodes"], stats["invalid_responses"]), (2, 0))
            self.assertEqual(set(pictures), {3})  # Every prompt carried the three camera pictures.
            self.assertEqual(verify(spec_path, directory / "results", directory / "verifier"),
                             {"success_rate": 1.0, "score": 1.0})


class LocalRunnerTests(unittest.TestCase):
    def test_local_environment_reports_failures_like_a_container(self):
        """A bridge that cannot be reached must not look like a definite answer."""
        with tempfile.TemporaryDirectory() as directory:
            environment = local.LocalEnvironment(f"http://127.0.0.1:{free_port()}", Path(directory) / "bridge")
            # The scripted solver reads the printed answer, as its subprocess version does.
            self.assertEqual(environment.call("status")["status"], "transport_error")
            command = "python3 /opt/robodojo/bridge_client.py"
            done = asyncio.run(environment.exec(command=f"{command} status"))
            self.assertEqual(done.return_code, 3)
            self.assertEqual(json.loads(done.stdout)["status"], "transport_error")
            agent = DirectAgent(logs_dir=Path(directory) / "agent", model_name="fake/vision-model")
            with self.assertRaisesRegex(DirectRunError, "failed with code 3"):
                asyncio.run(agent.player._bridge(HarborBridge(environment), "status"))
            # Wrong usage ends the real program with code 2; nothing else may run here.
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(asyncio.run(environment.exec(command=f"{command} act")).return_code, 2)
            with self.assertRaises(ValueError):
                asyncio.run(environment.exec(command="sh -c 'echo unsafe'"))
            # Two direct calls and the agent's own; the refused command was not recorded.
            self.assertEqual(len(environment.commands), 3)

    def test_fake_openai_mode_runs_the_agent_without_a_key(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict("os.environ"):
            output, printed = Path(directory) / "run", io.StringIO()
            with contextlib.redirect_stdout(printed):
                self.assertEqual(local.main(["--fake-openai", "--episodes", "1", "--output", str(output)]), 0)
            self.assertIn(fake_openai.SERVED_MODEL, printed.getvalue())
            self.assertIn("not a RoboDojo score", printed.getvalue())
            run = json.loads((output / "agent" / "robodojo_direct" / "run.json").read_text())
            self.assertEqual((run["model"], run["stats"]["episodes"]), ("openai/fake-vision-model", 1))
            self.assertTrue(any((output / "observations").iterdir()))
        for arguments in (["--fake-openai", "--model", "openai/x"], ["--fake-openai", "--api-base", "http://x"]):
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit):
                local.main(arguments)


class OpenAIWireFormatTests(unittest.TestCase):
    """The real agent and the real LiteLLM client against a fake OpenAI endpoint.

    Nothing is replaced inside the agent: requests leave over HTTP in the OpenAI
    Chat Completions format and the responses are parsed by the client library.
    """

    KEY_VARIABLE = "ROBODOJO_FAKE_OPENAI_KEY"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.spec_path = self.directory / "task_spec.json"
        self.spec_path.write_text(json.dumps(mock_task_spec(episodes=2)))
        port = free_port()
        threading.Thread(
            target=service.evaluate, args=(self.spec_path, self.directory / "results"),
            kwargs={"host": "127.0.0.1", "port": port}, daemon=True,
        ).start()
        self.environment = local.LocalEnvironment(f"http://127.0.0.1:{port}", self.directory / "bridge")
        deadline = time.monotonic() + 30
        while self.environment.call("status").get("status") == "transport_error":
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)
        self.fake = fake_openai.FakeOpenAI("fake-key-for-wire-test", flawed_first_answer=True)
        self.server = fake_openai.make_server(self.fake, "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.api_base = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def play(self, key):
        agent = DirectAgent(
            logs_dir=self.directory / "agent", model_name="openai/fake-vision-model",
            api_base=self.api_base, api_key_env=self.KEY_VARIABLE, api_retries=0, wait_sec=30,
        )
        context = AgentContext()

        async def run():
            await agent.setup(self.environment)
            await agent.run("unused", self.environment, context)

        with mock.patch.dict("os.environ", {self.KEY_VARIABLE: key}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                asyncio.run(run())
            finally:
                self.context = context
        return context

    def test_agent_plays_the_mock_through_the_openai_wire_format(self):
        direct = self.play("fake-key-for-wire-test").metadata["robodojo_direct"]
        self.assertEqual(self.fake.rejections, [])
        self.assertEqual(verify(self.spec_path, self.directory / "results", self.directory / "verifier"),
                         {"success_rate": 1.0, "score": 1.0})
        calls = len(self.fake.requests)
        stats = direct["stats"]
        # The first answer was out of bounds, so one call more than accepted decisions.
        self.assertEqual((stats["model_calls"], stats["invalid_responses"], stats["decisions"]),
                         (calls, 1, calls - 1))
        # Usage is read from the response body by the client library.
        self.assertEqual((stats["input_tokens"], stats["output_tokens"]),
                         (calls * fake_openai.PROMPT_TOKENS, calls * fake_openai.COMPLETION_TOKENS))
        # The served model and the endpoint come from the response, without the key.
        self.assertEqual(direct["served"]["models"], [fake_openai.SERVED_MODEL])
        self.assertEqual(direct["endpoint"], {"api_base": self.api_base, "environment": {}})
        self.assertEqual(direct["served"]["api_bases"], [self.api_base])
        self.assertNotIn("fake-key-for-wire-test", json.dumps(direct))
        for request in self.fake.requests:
            self.assertEqual(request["model"], "fake-vision-model")
            self.assertEqual((request["tool_choice"], request["parallel_tool_calls"]), ("required", False))
            self.assertEqual(request["tools"][0]["function"]["name"], "robodojo_act")
            _, pictures = fake_openai.newest_observation(request["messages"])
            self.assertEqual(len(pictures), 3)
        # The refused decision goes back as an assistant tool call and its tool message.
        retry = self.fake.requests[1]["messages"]
        self.assertEqual([message["role"] for message in retry], ["system", "user", "assistant", "tool"])
        self.assertEqual(retry[2]["tool_calls"][0]["id"], retry[3]["tool_call_id"])
        self.assertIn("rejected", retry[3]["content"])

        # Replay A's later request around B's first frame on a shared fake server.
        first, later = [], []
        for body in self.fake.requests:
            observation, _ = fake_openai.newest_observation(body["messages"])
            (first if observation["step_id"] == 0 else later).append(body)
        fake = fake_openai.FakeOpenAI("test")

        def answer(body):
            code, reply = fake.handle("/v1/chat/completions", "Bearer test", json.dumps(body).encode())
            self.assertEqual(code, 200)
            return reply["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]

        answer(first[0])
        before = answer(later[0])
        answer(first[-1])
        self.assertEqual(answer(later[0]), before)

    def test_a_refused_api_key_fails_the_run_without_a_reward(self):
        with self.assertRaisesRegex(DirectRunError, "Model API failed"):
            self.play("wrong-key")
        self.assertEqual(self.fake.requests, [])
        self.assertIn("Incorrect API key", self.fake.rejections[0])
        self.assertEqual(self.context.metadata["robodojo_direct"]["stats"]["decisions"], 0)
        with self.assertRaises(EvaluationError):
            verify(self.spec_path, self.directory / "results", self.directory / "verifier")

    def test_zero_retries_sends_one_http_request_on_server_error(self):
        with mock.patch.object(self.fake, "handle", return_value=(500, {"error": {"message": "busy"}})) as post:
            with self.assertRaisesRegex(DirectRunError, "after 1 attempts"):
                self.play("fake-key-for-wire-test")
        self.assertEqual(post.call_count, 1)
        stats = self.context.metadata["robodojo_direct"]["stats"]
        self.assertEqual((stats["api_attempts"], stats["model_calls"]), (1, 0))

    def test_unprefixed_model_records_the_endpoint_that_received_the_request(self):
        with mock.patch.dict("os.environ", {"OPENAI_BASE_URL": self.api_base}), \
                mock.patch.object(self.fake, "handle", return_value=(500, {"error": {"message": "busy"}})) as post:
            agent = DirectAgent(logs_dir=self.directory, model_name="gpt-4o", api_retries=0,
                                api_key_env=self.KEY_VARIABLE)
            with mock.patch.dict("os.environ", {self.KEY_VARIABLE: "throwaway"}), \
                    self.assertRaises(DirectRunError):
                asyncio.run(agent.player._complete([{"role": "user", "content": "test"}]))
            self.assertEqual(post.call_count, 1)
            self.assertEqual(agent.provenance()["endpoint"]["environment"]["OPENAI_BASE_URL"], self.api_base)

    def test_malformed_requests_are_refused_like_the_real_endpoint_would(self):
        tool = {"type": "function", "function": {"name": "robodojo_act", "parameters": {"type": "object"}}}
        valid = {
            "model": "fake-vision-model", "tools": [tool], "tool_choice": "required",
            "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        }
        fake_openai.check_request(valid)
        call = {"id": "c1", "type": "function", "function": {"name": "robodojo_act", "arguments": "{}"}}
        for change in (
            {"api_base": "http://leaked"}, {"tools": []}, {"tool_choice": "none"}, {"stream": True},
            {"parallel_tool_calls": "no"},
            {"messages": [{"role": "user", "content": "u"}, {"role": "user", "content": "u"}]},
            {"messages": valid["messages"] + [{"role": "assistant", "content": None, "tool_calls": [call]}]},
            {"messages": valid["messages"] + [{"role": "tool", "tool_call_id": "c1", "content": "x"}]},
            {"messages": valid["messages"] + [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "https://example.test/picture.png"}}]}]},
        ):
            with self.subTest(change=list(change)), self.assertRaises(fake_openai.RequestError):
                fake_openai.check_request({**valid, **change})


if __name__ == "__main__":
    unittest.main()
