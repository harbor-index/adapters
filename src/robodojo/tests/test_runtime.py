"""CPU-only checks of endpoint validation, native grading, and failure handling."""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from robodojo import runtime


def native_result(episodes=50, successes=10):
    details = {
        str(index): {
            "layout_id": index, "success": index < successes,
            "score": 1.0 if index < successes else 0.25,
        }
        for index in range(episodes)
    }
    return {
        "eval_time": episodes, "success_rate": successes / episodes,
        "score": (successes + (episodes - successes) * 0.25) / episodes * 100,
        "details": details,
    }


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.root = self.directory / "RoboDojo"
        self.root.mkdir()
        (self.root / ".harbor-upstream-revision").write_text(runtime.UPSTREAM_REVISION)
        (self.root / ".harbor-xpolicylab-revision").write_text(runtime.XPOLICYLAB_REVISION)
        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "robodojo.sh").write_text("# mock simulator\n")
        deploy = self.root / "XPolicyLab" / "policy" / "test_policy" / "deploy.py"
        deploy.parent.mkdir(parents=True)
        deploy.write_text("# installed upstream policy adapter\n")
        self.spec_path = self.directory / "task_spec.json"
        self.submission_path = self.directory / "submission.json"
        self.output = self.directory / "verifier"
        self.submission = {"policy_name": "test_policy", "host": "127.0.0.1", "port": 8000}
        self.write_json(self.submission_path, self.submission)
        self.spec = {
            "task_id": "insert_tubes", "dimension": "precision", "seed": 0,
            "upstream_revision": runtime.UPSTREAM_REVISION,
            "variants": [{"name": "insert_tubes", "episodes": 50}],
        }
        self.write_json(self.spec_path, self.spec)

    @staticmethod
    def write_json(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def result_for_command(self, command, environment, episodes=50):
        task = command[command.index("--task") + 1]
        variant = runtime.Variant(task, episodes)
        return runtime.result_path(
            self.root, variant, runtime.Submission(**self.submission),
            self.spec["seed"], environment["ROBODOJO_RUN_ID"],
        )

    def evaluate(self, runner):
        return runtime.evaluate(
            self.spec_path, self.submission_path, self.root, self.output, runner=runner,
        )

    def assert_no_reward(self):
        self.assertFalse((self.output / "reward.json").exists())
        self.assertFalse((self.output / "reward.txt").exists())
        self.assertFalse((self.output / "evaluation.json").exists())

    def test_native_result_percent_is_validated_and_reward_is_normalized(self):
        commands = []

        def runner(command, **kwargs):
            commands.append(command)
            self.assertEqual(kwargs["timeout"], 36_000)
            self.assertEqual(kwargs["env"]["EVAL_NUM"], "native")
            self.assertEqual(kwargs["cwd"], self.root)
            self.assertNotIn("shell", kwargs)
            self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
            kwargs["stdout"].write("native simulator output\n")
            path = self.result_for_command(command, kwargs["env"])
            self.write_json(path, native_result())
            (path.parent / "episode.txt").write_text("raw artifact")
            return subprocess.CompletedProcess(command, 0)

        with patch.dict(os.environ, {"EVAL_NUM": "1", "ROBODOJO_RUN_ID": "old-run"}):
            reward = self.evaluate(runner)
        self.assertEqual(reward, {"success_rate": 0.2, "score": 0.4})
        self.assertEqual(json.loads((self.output / "reward.json").read_text()), reward)
        evaluation = json.loads((self.output / "evaluation.json").read_text())
        self.assertEqual(evaluation["task_id"], "insert_tubes")
        self.assertEqual(evaluation["dimension"], "precision")
        self.assertEqual(evaluation["seed"], 0)
        self.assertEqual(evaluation["upstream_revision"], runtime.UPSTREAM_REVISION)
        self.assertEqual(evaluation["xpolicylab_revision"], runtime.XPOLICYLAB_REVISION)
        self.assertEqual(evaluation["submission"], self.submission)
        self.assertEqual(evaluation["rewards"], reward)
        self.assertEqual(len(evaluation["variants"]), 1)
        self.assertEqual(evaluation["variants"][0]["episodes"], 50)
        self.assertTrue(Path(evaluation["variants"][0]["result_path"]).is_file())
        self.assertIn("--eval-num", commands[0])
        self.assertEqual(commands[0][-4:], ["--ckpt", "external", "--action-type", "ee"])
        self.assertEqual(len(list((self.output / "artifacts").rglob("_result.json"))), 1)
        self.assertEqual(len(list((self.output / "artifacts").rglob("episode.txt"))), 1)
        self.assertIn("native simulator output", (self.output / "insert_tubes.log").read_text())

    def test_generalization_runs_both_halves_sequentially_with_unique_ids(self):
        self.spec.update(
            task_id="stack_bowls", dimension="generalization",
            variants=[{"name": "stack_bowls", "episodes": 25},
                      {"name": "stack_bowls_random", "episodes": 25}],
        )
        self.write_json(self.spec_path, self.spec)
        calls, run_ids = [], []

        def runner(command, **kwargs):
            task = command[command.index("--task") + 1]
            calls.append(task)
            run_ids.append(kwargs["env"]["ROBODOJO_RUN_ID"])
            self.write_json(
                self.result_for_command(command, kwargs["env"], 25),
                native_result(25, 5 if len(calls) == 1 else 10),
            )
            return subprocess.CompletedProcess(command, 0)

        self.assertEqual(self.evaluate(runner), {"success_rate": 0.3, "score": 0.475})
        self.assertEqual(calls, ["stack_bowls", "stack_bowls_random"])
        self.assertEqual(len(set(run_ids)), 2)

    def test_partial_result_preserves_raw_artifact_but_has_no_reward(self):
        def runner(command, **kwargs):
            self.write_json(self.result_for_command(command, kwargs["env"]), native_result(1, 0))
            return subprocess.CompletedProcess(command, 0)

        with self.assertRaisesRegex(runtime.EvaluationError, "incomplete"):
            self.evaluate(runner)
        self.assert_no_reward()
        self.assertEqual(len(list((self.output / "artifacts").rglob("_result.json"))), 1)

    def test_native_recovery_can_use_replacement_layouts(self):
        def runner(command, **kwargs):
            result = native_result()
            # Upstream abandons broken or unstable layouts and refills from
            # its seed queue; the replacement can be beyond the episode count.
            result["details"]["0"]["layout_id"] = 50
            result["details"]["2"]["layout_id"] = 53
            self.write_json(self.result_for_command(command, kwargs["env"]), result)
            return subprocess.CompletedProcess(command, 0)

        self.assertEqual(self.evaluate(runner), {"success_rate": 0.2, "score": 0.4})

    def test_subprocess_error_and_timeout_never_become_zero_reward(self):
        for failure in (
            subprocess.CalledProcessError(1, ["bash"]),
            subprocess.TimeoutExpired(["bash"], 36_000),
        ):
            with self.subTest(failure=type(failure).__name__):
                self.output.mkdir(exist_ok=True)
                (self.output / "reward.json").write_text('{"score": 1}')
                (self.output / "reward.txt").write_text("1")
                (self.output / "evaluation.json").write_text('{"rewards": {"score": 1}}')

                def runner(command, **kwargs):
                    kwargs["stdout"].write("simulator failed\n")
                    raise failure

                with self.assertRaisesRegex(runtime.EvaluationError, "Simulator failed"):
                    self.evaluate(runner)
                self.assert_no_reward()
                self.assertIn("simulator failed", (self.output / "insert_tubes.log").read_text())

    def test_old_run_does_not_satisfy_current_invocation(self):
        old = runtime.result_path(
            self.root, runtime.Variant("insert_tubes", 50),
            runtime.Submission(**self.submission), 0, "old-run",
        )
        self.write_json(old, native_result())
        with self.assertRaisesRegex(runtime.EvaluationError, "exactly one native result"):
            self.evaluate(lambda command, **kwargs: subprocess.CompletedProcess(command, 0))
        self.assert_no_reward()

    def test_stale_result_with_current_id_is_rejected(self):
        def runner(command, **kwargs):
            path = self.result_for_command(command, kwargs["env"])
            self.write_json(path, native_result())
            os.utime(path, ns=(1, 1))
            return subprocess.CompletedProcess(command, 0)

        with self.assertRaisesRegex(runtime.EvaluationError, "stale"):
            self.evaluate(runner)
        self.assert_no_reward()

    def test_multiple_results_for_same_invocation_are_rejected(self):
        def runner(command, **kwargs):
            path = self.result_for_command(command, kwargs["env"])
            self.write_json(path, native_result())
            self.write_json(path.parent / "duplicate" / "_result.json", native_result())
            return subprocess.CompletedProcess(command, 0)

        with self.assertRaisesRegex(runtime.EvaluationError, "exactly one native result"):
            self.evaluate(runner)
        self.assert_no_reward()

    def test_symlinked_result_is_rejected(self):
        def runner(command, **kwargs):
            path = self.result_for_command(command, kwargs["env"])
            path.parent.mkdir(parents=True)
            target = self.directory / "submitted-result.json"
            self.write_json(target, native_result())
            path.symlink_to(target)
            return subprocess.CompletedProcess(command, 0)

        with self.assertRaisesRegex(runtime.EvaluationError, "symlinks"):
            self.evaluate(runner)
        self.assert_no_reward()

    def test_malformed_native_result_has_no_reward(self):
        def runner(command, **kwargs):
            path = self.result_for_command(command, kwargs["env"])
            path.parent.mkdir(parents=True)
            path.write_text('{"eval_time": 50, "score": NaN}')
            return subprocess.CompletedProcess(command, 0)

        with self.assertRaisesRegex(runtime.EvaluationError, "Non-finite"):
            self.evaluate(runner)
        self.assert_no_reward()

    def test_wrong_image_revision_stops_before_simulator(self):
        (self.root / ".harbor-upstream-revision").write_text("wrong revision")
        with self.assertRaisesRegex(runtime.EvaluationError, "pinned upstream revision"):
            self.evaluate(lambda *args, **kwargs: self.fail("Simulator must not run"))
        self.assert_no_reward()

    def test_wrong_xpolicylab_revision_stops_before_simulator(self):
        (self.root / ".harbor-xpolicylab-revision").write_text("wrong revision")
        with self.assertRaisesRegex(runtime.EvaluationError, "xpolicylab-revision"):
            self.evaluate(lambda *args, **kwargs: self.fail("Simulator must not run"))
        self.assert_no_reward()

    def test_native_detail_and_aggregate_validation(self):
        original = native_result()
        mutations = (
            lambda value: value.update(eval_time=True),
            lambda value: value["details"].pop("0"),
            lambda value: value["details"]["0"].update(layout_id=1),
            lambda value: value["details"]["0"].update(layout_id=-1),
            lambda value: value["details"]["0"].update(layout_id=False),
            lambda value: value["details"]["0"].update(success=1),
            lambda value: value["details"]["0"].update(score=True),
            lambda value: value["details"]["0"].update(score=0.25),
            lambda value: value["details"]["0"].update(score=float("nan")),
            lambda value: value["details"]["0"].update(score=1.01),
            lambda value: value.update(success_rate=1),
            lambda value: value.update(score=0.4),
            lambda value: value.update(score=10 ** 1000),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                value = deepcopy(original)
                mutate(value)
                with self.assertRaises(runtime.EvaluationError):
                    runtime.validate_result(value, 50)

    def test_submission_accepts_only_safe_endpoint_fields(self):
        invalid = (
            {"policy_name": "../evil"}, {"policy_name": "package.policy"},
            {"policy_name": "class"}, {"host": "127.0.0.1;touch /tmp/no"},
            {"host": "$(echo bad)"}, {"host": "https://localhost"},
            {"host": "a,b"}, {"host": "-option"}, {"host": "999.1.1.1"},
            {"port": True}, {"port": 0}, {"port": 65536}, {"port": "8000"},
            {"score": 1},
        )
        for changed in invalid:
            with self.subTest(changed=changed):
                self.write_json(self.submission_path, {**self.submission, **changed})
                with self.assertRaises(runtime.EvaluationError):
                    runtime.load_submission(self.submission_path)
        for host in ("localhost", "host.docker.internal", "policy-1.example.org", "127.0.0.1"):
            self.write_json(self.submission_path, {**self.submission, "host": host})
            self.assertEqual(runtime.load_submission(self.submission_path).host, host)

    def test_task_spec_cannot_override_native_protocol(self):
        invalid = (
            {"task_id": "../../evil"}, {"dimension": "memory"},
            {"seed": True}, {"seed": -1}, {"upstream_revision": "main"},
            {"variants": [{"name": "insert_tubes", "episodes": 1}]},
            {"variants": [{"name": "insert_tubes", "episodes": 50.0}]},
        )
        for changed in invalid:
            with self.subTest(changed=changed):
                self.write_json(self.spec_path, {**self.spec, **changed})
                with self.assertRaises(runtime.EvaluationError):
                    runtime.load_spec(self.spec_path)

    def test_duplicate_json_keys_are_rejected(self):
        self.submission_path.write_text('{"host": "localhost", "host": "evil"}')
        with self.assertRaisesRegex(runtime.EvaluationError, "Duplicate JSON key"):
            runtime.load_submission(self.submission_path)


if __name__ == "__main__":
    unittest.main()
