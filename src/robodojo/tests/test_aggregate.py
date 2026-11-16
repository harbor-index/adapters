"""Check native dimension weighting and rejection of incomplete Harbor jobs."""

from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from robodojo.aggregate import aggregate
from robodojo.catalog import DIMENSION_TASKS, UPSTREAM_REVISION, XPOLICYLAB_REVISION, task_spec
from robodojo.runtime import EvaluationError


class AggregateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.job = Path(self.temporary.name) / "job"
        self.endpoint = {"policy_name": "test_policy", "host": "policy.example.org", "port": 9999}
        self.records = {}
        index = 0
        for dimension, names in DIMENSION_TASKS.items():
            for task_id in names:
                trial_dir = self.job / f"trial-{index:02}"
                reward = float(dimension == "generalization")
                rewards = {"success_rate": reward, "score": reward}
                report = task_spec(task_id, 0)
                report.update(
                    xpolicylab_revision=XPOLICYLAB_REVISION,
                    submission=deepcopy(self.endpoint), rewards=rewards,
                )
                report["variants"] = [
                    {**variant, "run_id": f"harbor_{index:032x}_{variant_index}",
                     "result_path": str(trial_dir / "verifier" / "artifacts" /
                                        variant["name"] / f"run-{index}-{variant_index}" /
                                        "_result.json")}
                    for variant_index, variant in enumerate(report["variants"])
                ]
                trial = {
                    "task_name": f"robodojo/{task_id.lower().replace('_', '-')}",
                    "exception_info": None, "verifier_result": {"rewards": rewards},
                }
                self.records[task_id] = (trial_dir, trial, report)
                self.save(task_id)
                index += 1

    def save(self, task_id):
        directory, trial, report = self.records[task_id]
        (directory / "verifier").mkdir(parents=True, exist_ok=True)
        (directory / "result.json").write_text(json.dumps(trial))
        (directory / "verifier" / "evaluation.json").write_text(json.dumps(report))

    def test_full_native_provenance_uses_equal_dimension_weight(self):
        result = aggregate(self.job)
        self.assertEqual(result["tasks"], 42)
        self.assertEqual(result["success_rate"], 0.2)
        self.assertEqual(result["score"], 0.2)
        self.assertNotEqual(result["success_rate"], 12 / 42)
        self.assertEqual(result["dimensions"]["generalization"], {"success_rate": 1, "score": 1})
        for dimension in ("memory", "precision", "long-horizon", "open"):
            self.assertEqual(result["dimensions"][dimension], {"success_rate": 0, "score": 0})
        self.assertEqual(result["seed"], 0)
        self.assertEqual(result["submission"], self.endpoint)
        self.assertEqual(result["upstream_revision"], UPSTREAM_REVISION)

    def test_incomplete_or_repeated_task_cannot_produce_full_benchmark_score(self):
        directory, _, _ = self.records["insert_tubes"]
        (directory / "result.json").unlink()
        with self.assertRaisesRegex(EvaluationError, "42 tasks"):
            aggregate(self.job)
        self.save("insert_tubes")
        shutil.copytree(directory, self.job / "duplicate-trial")
        with self.assertRaisesRegex(EvaluationError, "repeated"):
            aggregate(self.job)

    def test_empty_job_is_rejected(self):
        empty = self.job / "empty"
        empty.mkdir()
        with self.assertRaisesRegex(EvaluationError, "No Harbor trial"):
            aggregate(empty)

    def test_seed_and_policy_endpoint_must_match_across_tasks(self):
        _, _, report = self.records["insert_tubes"]
        report["seed"] = 1
        self.save("insert_tubes")
        with self.assertRaisesRegex(EvaluationError, "separately"):
            aggregate(self.job)
        report["seed"] = 0
        report["submission"]["host"] = "different-policy.example.org"
        self.save("insert_tubes")
        with self.assertRaisesRegex(EvaluationError, "separately"):
            aggregate(self.job)

    def test_failed_trial_is_not_averaged_as_policy_failure(self):
        _, trial, _ = self.records["insert_tubes"]
        trial["exception_info"] = {"exception_type": "VerifierTimeoutError"}
        self.save("insert_tubes")
        with self.assertRaisesRegex(EvaluationError, "Trial failed"):
            aggregate(self.job)

    def test_trial_task_identity_must_match_native_report(self):
        _, trial, _ = self.records["insert_tubes"]
        trial["task_name"] = "robodojo/align-blocks"
        self.save("insert_tubes")
        with self.assertRaisesRegex(EvaluationError, "identity mismatch"):
            aggregate(self.job)

    def test_simulator_and_policy_code_revisions_are_verified(self):
        _, _, report = self.records["insert_tubes"]
        original = deepcopy(report)
        for field in ("upstream_revision", "xpolicylab_revision"):
            with self.subTest(field=field):
                report.clear()
                report.update(deepcopy(original))
                report[field] = "0" * 40
                self.save("insert_tubes")
                with self.assertRaises(EvaluationError):
                    aggregate(self.job)

    def test_native_task_dimension_and_complete_variant_protocol_are_required(self):
        _, _, report = self.records["stack_bowls"]
        original = deepcopy(report)
        mutations = (
            {"dimension": "precision"},
            {"variants": report["variants"][:1]},
            {"variants": [{**variant, "episodes": 1} for variant in report["variants"]]},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                report.clear()
                report.update(deepcopy(original))
                report.update(deepcopy(mutation))
                self.save("stack_bowls")
                with self.assertRaises(EvaluationError):
                    aggregate(self.job)

    def test_rewards_must_match_native_provenance(self):
        _, trial, _ = self.records["insert_tubes"]
        trial["verifier_result"]["rewards"] = {"success_rate": 0.5, "score": 0.5}
        self.save("insert_tubes")
        with self.assertRaisesRegex(EvaluationError, "does not match"):
            aggregate(self.job)

    def test_invalid_reward_values_and_missing_metric_are_rejected(self):
        _, trial, report = self.records["insert_tubes"]
        for rewards in (
            {"success_rate": True, "score": 0.5},
            {"success_rate": -0.1, "score": 0.5},
            {"success_rate": 0.5, "score": 100},
            {"success_rate": float("nan"), "score": 0.5},
            {"success_rate": 0.5},
        ):
            with self.subTest(rewards=rewards):
                trial["verifier_result"]["rewards"] = rewards
                report["rewards"] = rewards
                self.save("insert_tubes")
                with self.assertRaises(EvaluationError):
                    aggregate(self.job)

    def test_missing_native_report_is_rejected(self):
        directory, _, _ = self.records["insert_tubes"]
        (directory / "verifier" / "evaluation.json").unlink()
        with self.assertRaises(EvaluationError):
            aggregate(self.job)


if __name__ == "__main__":
    unittest.main()
