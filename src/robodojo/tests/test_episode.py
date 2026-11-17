"""Checks of the simulator-side episode loop against a fake native environment."""

import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest

from robodojo.direct.broker import Broker
from robodojo.direct.episode import DirectAbort, joint_action, run_episode

from helpers import FakeEnv, FakeKinematics, hold, model_call

POLICY = Path(__file__).parents[1] / "src" / "robodojo" / "direct" / "policy"


def load_model():
    spec = importlib.util.spec_from_file_location("harbor_direct_model", POLICY / "model.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EpisodeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.results = Path(self.temporary.name) / "results"
        self.broker = Broker(self.results, poll_interval=0.01)
        self.model = load_model().Model({}, broker=self.broker, serve=False)
        self.call = model_call(self.model)
        self.errors = []

    def agent(self, decide, limit=50):
        """Answer observations until the episode ends, as the Harbor agent would."""
        def loop():
            try:
                result = self.broker.observe(10)
                for _ in range(limit):
                    if result["status"] != "observation":
                        return
                    result = self.broker.act(decide(result["observation"]), 10)
            except Exception as error:
                self.errors.append(error)
        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        return thread

    def run_env(self, env, decide=hold):
        thread = self.agent(decide)
        try:
            run_episode(env, self.call, kinematics_factory=FakeKinematics, sleep=lambda seconds: None)
        finally:
            thread.join(10)
        self.assertEqual(self.errors, [])

    def episodes(self):
        return [json.loads(line) for line in (self.results / "broker" / "episodes.jsonl").read_text().splitlines()]

    def test_episode_runs_to_the_native_step_limit(self):
        env = FakeEnv(step_lim=12, layout_id=4)
        self.run_env(env)
        # Twelve control steps in decisions of five steps: 5 + 5 + 2.
        self.assertEqual(env.take_action_cnt[0], 12)
        record = self.episodes()[0]
        self.assertEqual((record["decisions"], record["executed_steps"], record["control_steps"]), (3, 12, 12))
        self.assertEqual((record["layout_id"], record["variant"]), (4, "insert_tubes"))
        self.assertIs(record["native_success_observed"], False)
        self.assertEqual(record["control_dt"], 0.04)
        events = [json.loads(line) for line in
                  (self.results / "broker" / "decisions" / "0000.jsonl").read_text().splitlines()]
        self.assertEqual([event["event"] for event in events], ["accepted", "executed"] * 3)
        self.assertEqual([event["executed_steps"] for event in events if event["event"] == "executed"], [5, 5, 2])

    def test_native_success_ends_the_episode_inside_a_decision(self):
        env = FakeEnv(step_lim=100, succeed_at=3)
        self.run_env(env)
        self.assertEqual(env.take_action_cnt[0], 3)
        record = self.episodes()[0]
        self.assertEqual((record["decisions"], record["executed_steps"]), (1, 3))
        self.assertIs(record["native_success_observed"], True)

    def test_actions_use_native_joint_keys_and_gripper_convention(self):
        env = FakeEnv(step_lim=1)

        def close_left(observation):
            decision = hold(observation, steps=1)
            decision["target"]["left"]["gripper_closed"] = True
            return decision

        self.run_env(env, close_left)
        action = env.actions[0]
        self.assertEqual(sorted(action), [
            "left_arm_joint_state", "left_ee_joint_state",
            "right_arm_joint_state", "right_ee_joint_state",
        ])
        self.assertEqual((action["left_arm_joint_state"].shape, action["left_ee_joint_state"].shape), ((6,), (1,)))
        self.assertEqual((float(action["left_ee_joint_state"][0]), float(action["right_ee_joint_state"][0])), (0.0, 1.0))
        for bad in ([0.0] * 13, [0.0] * 6 + [1.5] + [0.0] * 7, [float("nan")] * 14):
            with self.assertRaises(DirectAbort):
                joint_action(bad)

    def test_agent_only_sees_instruction_frames_and_robot_state(self):
        seen = []

        def record(observation):
            seen.append(observation)
            return hold(observation)

        self.run_env(FakeEnv(step_lim=5), record)
        observation = seen[0]
        self.assertEqual(observation["cameras"], ["cam_head", "cam_left_wrist", "cam_right_wrist"])
        self.assertEqual(observation["instruction"], "Insert the tubes into the rack.")
        self.assertEqual(set(observation), {
            "request_id", "episode", "decision_index", "cameras", "image_sha256", "previous_result",
            "instruction", "step_id", "max_episode_steps", "remaining_steps", "joint_state",
            "current_eef", "frame", "eef_link",
        })

    def test_empty_native_conditions_abort_instead_of_scoring(self):
        env = FakeEnv()
        env.reward_manager.check_list = [[]]
        with self.assertRaisesRegex(DirectAbort, "vacuous"):
            run_episode(env, self.call, kinematics_factory=FakeKinematics)
        self.assertFalse((self.results / "broker" / "episodes.jsonl").exists())

    def test_failed_kinematics_check_aborts_before_any_step(self):
        class Broken(FakeKinematics):
            def check(self):
                raise ValueError("Robot kinematics do not match")

        env = FakeEnv()
        with self.assertRaisesRegex(DirectAbort, "Kinematics validation failed"):
            run_episode(env, self.call, kinematics_factory=Broken)
        self.assertEqual(env.take_action_cnt[0], 0)

    def test_agent_abort_stops_the_native_process(self):
        env = FakeEnv()
        aborter = threading.Thread(target=lambda: (self.broker.observe(10), self.broker.abort("limit reached")))
        aborter.start()
        with self.assertRaisesRegex(DirectAbort, "limit reached"):
            run_episode(env, self.call, kinematics_factory=FakeKinematics, sleep=lambda seconds: None)
        aborter.join(10)
        self.assertIsInstance(DirectAbort("x"), SystemExit)
        self.assertEqual(env.take_action_cnt[0], 0)

    def test_native_exception_propagates_and_marks_the_episode_unscored(self):
        env = FakeEnv(step_lim=20)
        env.raise_on_step = 2
        thread = self.agent(hold)
        with self.assertRaisesRegex(RuntimeError, "native physics fault"):
            run_episode(env, self.call, kinematics_factory=FakeKinematics, sleep=lambda seconds: None)
        thread.join(10)
        record = self.episodes()[0]
        self.assertEqual(record["ended_by"], "native_exception")
        self.assertIn("native physics fault", record["error"])
        # Native recovery may continue with another layout afterwards.
        self.assertEqual(self.broker.status()["state"], "idle")

    def test_policy_channel_failure_aborts(self):
        def broken(func_name, obs=None):
            raise ConnectionError("websocket closed")

        with self.assertRaisesRegex(DirectAbort, "Policy channel failed"):
            run_episode(FakeEnv(), broken, kinematics_factory=FakeKinematics)

    def test_policy_has_no_autonomous_action_path(self):
        with self.assertRaises(RuntimeError):
            self.model.get_action()
        with self.assertRaises(RuntimeError):
            self.model.update_obs({})
        self.assertIsNone(self.model.reset())
        deploy = (POLICY / "deploy.py").read_text()
        self.assertIn("def eval_one_episode(TASK_ENV, model_client):", deploy)
        self.assertIn("eval_batch: false", (POLICY / "deploy.yml").read_text())


if __name__ == "__main__":
    unittest.main()
