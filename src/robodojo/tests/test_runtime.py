"""CPU-only checks of the validation shared by supervisor, verifier and aggregation."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from robodojo import runtime

from helpers import native_result, task_spec_dict


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.spec_path = self.directory / "task_spec.json"
        self.spec = task_spec_dict()
        self.spec_path.write_text(json.dumps(self.spec))

    def test_native_totals_are_returned_unnormalized(self):
        self.assertEqual(runtime.validate_result(native_result(), 50), (10, 20.0))
        self.assertEqual(runtime.validate_result(native_result(25, 25), 25), (25, 25.0))

    def test_native_recovery_can_use_replacement_layouts(self):
        result = native_result()
        # Upstream abandons broken or unstable layouts and refills from its seed
        # queue; the replacement can be beyond the episode count.
        result["details"]["0"]["layout_id"] = 50
        result["details"]["2"]["layout_id"] = 53
        self.assertEqual(runtime.validate_result(result, 50), (10, 20.0))

    def test_native_detail_and_aggregate_validation(self):
        original = native_result()
        mutations = (
            lambda value: value.update(eval_time=True),
            lambda value: value.update(eval_time=49),
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
            lambda value: value.update(details=[]),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                value = deepcopy(original)
                mutate(value)
                with self.assertRaises(runtime.EvaluationError):
                    runtime.validate_result(value, 50)

    def test_task_spec_is_loaded_with_the_native_protocol(self):
        spec = runtime.load_spec(self.spec_path)
        self.assertEqual((spec.task_id, spec.dimension, spec.seed), ("insert_tubes", "precision", 0))
        self.assertEqual(spec.variants, (runtime.Variant("insert_tubes", 50),))
        self.spec_path.write_text(json.dumps(task_spec_dict("stack_bowls", "generalization", 2)))
        spec = runtime.load_spec(self.spec_path)
        self.assertEqual(
            spec.variants, (runtime.Variant("stack_bowls", 25), runtime.Variant("stack_bowls_random", 25)),
        )

    def test_task_spec_cannot_override_native_protocol(self):
        invalid = (
            {"task_id": "../../evil"}, {"dimension": "memory"},
            {"seed": True}, {"seed": -1}, {"upstream_revision": "main"},
            {"variants": [{"name": "insert_tubes", "episodes": 1}]},
            {"variants": [{"name": "insert_tubes", "episodes": 50.0}]},
        )
        for changed in invalid:
            with self.subTest(changed=changed):
                self.spec_path.write_text(json.dumps({**self.spec, **changed}))
                with self.assertRaises(runtime.EvaluationError):
                    runtime.load_spec(self.spec_path)

    def test_json_reader_rejects_ambiguous_or_nonstandard_input(self):
        path = self.directory / "value.json"
        for text, pattern in (
            ('{"seed": 0, "seed": 1}', "Duplicate JSON key"),
            ('{"score": NaN}', "Non-finite"),
            ('{"score": Infinity}', "Non-finite"),
            ("[1, 2]", "Expected a JSON object"),
            ("{not json", "Cannot read JSON"),
        ):
            with self.subTest(text=text):
                path.write_text(text)
                with self.assertRaisesRegex(runtime.EvaluationError, pattern):
                    runtime.read_json(path)
        with self.assertRaisesRegex(runtime.EvaluationError, "Cannot read JSON"):
            runtime.read_json(self.directory / "missing.json")

    def test_json_writer_is_atomic_and_rejects_non_finite_numbers(self):
        path = self.directory / "out.json"
        runtime.write_json(path, {"score": 0.5})
        self.assertEqual(runtime.read_json(path), {"score": 0.5})
        self.assertEqual([item.name for item in self.directory.glob("out.json*")], ["out.json"])
        with self.assertRaises(ValueError):
            runtime.write_json(path, {"score": float("nan")})
        self.assertEqual(runtime.read_json(path), {"score": 0.5})


if __name__ == "__main__":
    unittest.main()
