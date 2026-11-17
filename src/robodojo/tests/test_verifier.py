"""Checks that rewards come only from complete, consistent simulator results."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from robodojo.catalog import XPOLICYLAB_REVISION
from robodojo.runtime import EvaluationError
from robodojo.verifier import verify

from helpers import native_result, task_spec_dict


class VerifierTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.results = self.directory / "results"
        self.output = self.directory / "verifier"
        self.spec_path = self.directory / "task_spec.json"
        self.build(task_spec_dict())

    def build(self, spec, successes=10):
        self.spec = spec
        self.spec_path.write_text(json.dumps(spec))
        self.native, self.ledger = {}, []
        first = 0
        for variant in spec["variants"]:
            native = native_result(variant["episodes"], successes * variant["episodes"] // 50)
            self.native[variant["name"]] = native
            for detail in native["details"].values():
                self.ledger.append({
                    "episode_index": first, "variant": variant["name"],
                    "layout_id": detail["layout_id"], "layout_seed_group": spec["seed"],
                    "max_episode_steps": 400, "contract_version": "direct-eef-v1",
                    "decisions": 80, "executed_steps": 400 if not detail["success"] else 120,
                    "control_steps": 400 if not detail["success"] else 120,
                    "ended_by": "native_termination", "native_success_observed": detail["success"],
                })
                first += 1
        self.verdict = {
            "status": "completed", **{key: spec[key] for key in (
                "task_id", "dimension", "seed", "upstream_revision")},
            "xpolicylab_revision": XPOLICYLAB_REVISION, "policy_name": "HarborDirect",
            "variants": [{**variant, "run_id": f"harbor_{index}"}
                         for index, variant in enumerate(spec["variants"])],
        }
        self.save()

    def save(self):
        (self.results / "broker").mkdir(parents=True, exist_ok=True)
        (self.results / "supervisor.json").write_text(json.dumps(self.verdict))
        (self.results / "broker" / "episodes.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in self.ledger),
        )
        for name, native in self.native.items():
            path = self.results / "native" / name / "_result.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(native))

    def assert_no_reward(self, pattern):
        self.save()
        with self.assertRaisesRegex(EvaluationError, pattern):
            verify(self.spec_path, self.results, self.output)
        for name in ("reward.json", "reward.txt", "evaluation.json"):
            self.assertFalse((self.output / name).exists())

    def test_reward_is_normalized_from_the_native_result(self):
        reward = verify(self.spec_path, self.results, self.output)
        self.assertEqual(reward, {"success_rate": 0.2, "score": 0.4})
        self.assertEqual(json.loads((self.output / "reward.json").read_text()), reward)
        evaluation = json.loads((self.output / "evaluation.json").read_text())
        self.assertEqual(evaluation["rewards"], reward)
        self.assertEqual(evaluation["policy_name"], "HarborDirect")
        self.assertEqual(evaluation["contract_version"], "direct-eef-v1")
        self.assertEqual(evaluation["variants"][0]["decisions"], 50 * 80)
        self.assertEqual(evaluation["variants"][0]["control_steps"], 10 * 120 + 40 * 400)

    def test_generalization_combines_both_variants(self):
        self.build(task_spec_dict("stack_bowls", "generalization", seed=1), successes=20)
        reward = verify(self.spec_path, self.results, self.output)
        self.assertEqual(reward, {"success_rate": 0.4, "score": 0.55})
        evaluation = json.loads((self.output / "evaluation.json").read_text())
        self.assertEqual([variant["name"] for variant in evaluation["variants"]],
                         ["stack_bowls", "stack_bowls_random"])
        self.assertEqual(evaluation["seed"], 1)

    def test_stale_reward_is_removed_before_any_check(self):
        self.output.mkdir()
        (self.output / "reward.json").write_text('{"score": 1}')
        (self.output / "reward.txt").write_text("1")
        self.verdict = {"status": "failed", "error": "Native simulator client failed"}
        self.assert_no_reward("did not complete")

    def test_missing_or_aborted_runs_have_no_reward(self):
        (self.results / "supervisor.json").unlink()
        with self.assertRaisesRegex(EvaluationError, "Cannot read JSON"):
            verify(self.spec_path, self.results, self.output)
        self.save()
        (self.results / "broker" / "abort.json").write_text('{"reason": "spending limit"}')
        self.assert_no_reward("aborted")

    def test_provenance_must_match_the_task(self):
        original = deepcopy(self.verdict)
        for field, value in (
            ("task_id", "align_blocks"), ("dimension", "open"), ("seed", 1),
            ("upstream_revision", "0" * 40), ("xpolicylab_revision", "0" * 40),
            ("policy_name", "demo_policy"), ("variants", []),
            ("variants", [{"name": "insert_tubes", "episodes": 25}]),
        ):
            with self.subTest(field=field, value=value):
                self.verdict = {**deepcopy(original), field: value}
                self.assert_no_reward("unexpected|complete native protocol")

    def test_incomplete_native_result_has_no_reward(self):
        self.native["insert_tubes"] = native_result(49, 10)
        self.assert_no_reward("incomplete")

    def test_scored_episode_needs_a_matching_direct_episode(self):
        original = deepcopy(self.ledger)
        cases = (
            (lambda ledger: ledger.pop(3), "no Direct episode"),
            (lambda ledger: ledger[3].update(ended_by="native_exception"), "native termination"),
            (lambda ledger: ledger[3].update(native_success_observed=False), "disagree"),
            (lambda ledger: ledger[3].update(layout_seed_group=2), "seed group"),
            (lambda ledger: ledger[3].update(executed_steps=119), "not all covered"),
            (lambda ledger: ledger[3].update(control_steps=401, executed_steps=401), "impossible"),
            (lambda ledger: ledger[3].update(variant="other_task"), "no Direct episode"),
            (lambda ledger: ledger.append(dict(ledger[3])), "repeats"),
            (lambda ledger: ledger[3].update(contract_version="other"), "one action contract"),
        )
        for mutate, pattern in cases:
            with self.subTest(pattern=pattern):
                self.ledger = deepcopy(original)
                mutate(self.ledger)
                self.assert_no_reward(pattern)

    def test_unscored_extra_episodes_are_allowed(self):
        # Native recovery can drop a layout after its episode already ran.
        self.native["insert_tubes"]["details"]["0"]["layout_id"] = 50
        self.ledger[0]["ended_by"] = "native_exception"
        self.ledger.append({**self.ledger[1], "layout_id": 50, "native_success_observed": True,
                            "executed_steps": 120, "control_steps": 120, "episode_index": 50})
        self.save()
        self.assertEqual(verify(self.spec_path, self.results, self.output), {"success_rate": 0.2, "score": 0.4})

    def test_corrupt_ledger_is_rejected(self):
        self.save()
        (self.results / "broker" / "episodes.jsonl").write_text("{not json\n")
        with self.assertRaisesRegex(EvaluationError, "corrupt"):
            verify(self.spec_path, self.results, self.output)
        (self.results / "broker" / "episodes.jsonl").unlink()
        with self.assertRaisesRegex(EvaluationError, "Cannot read the broker ledger"):
            verify(self.spec_path, self.results, self.output)


if __name__ == "__main__":
    unittest.main()
