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

from harbor.models.agent.context import AgentContext

from robodojo.adapter import MOCK_HARBOR_NAME, RoboDojoAdapter
from robodojo.aggregate import aggregate
from robodojo.catalog import MOCK_TASK_ID, mock_task_spec
from robodojo.direct import supervisor
from robodojo.direct.broker import encode_png
from robodojo.direct.contract import STEP_TRANSLATION_M
from robodojo.direct.episode import joint_action
from robodojo.direct_agent import DirectAgent
from robodojo.mock import local, scene, service, solver
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


if __name__ == "__main__":
    unittest.main()
