"""Offline tests for the TavernBench adapter (no network, no Docker).

Run from src/tavernbench:  uv run python -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from tavernbench.adapter import (
    TEMPLATE_DIR,
    TavernBenchAdapter,
    TavernBenchTask,
    oracle_answer,
    sanitize_task_id,
)

_spec = importlib.util.spec_from_file_location(
    "tavernbench_score", TEMPLATE_DIR / "tests" / "score.py"
)
score = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(score)

# One synthetic label per scoring rule, shaped like the released labels.
LABELS = [
    {"id": "X1-000", "scoring": "exact", "answer": "win"},
    {"id": "X1-001", "scoring": "exact", "answer": [2, 0, 1]},
    {"id": "X2-000", "scoring": "set", "answer": ["B", "D", "F"]},
    {
        "id": "X3-000",
        "scoring": "brier",
        "answer": {"win": 0.75, "tie": 0.2, "loss": 0.05},
    },
    {"id": "X4-000", "scoring": "numeric_rel", "answer": 5.8, "tol": 0.25},
    {"id": "X5-000", "scoring": "numeric_abs", "answer": 0.77, "tol": 0.15},
    {
        "id": "X6-000",
        "scoring": "value_map",
        "answer": 0.69,
        "tol": 0.03,
        "values": {"": 0.2, "0,2": 0.69, "2,0": 0.67},
    },
    {
        "id": "X7-000",
        "scoring": "value_map_str",
        "answer": 0.27,
        "tol": 0.03,
        "values": {"ABC": 0.1, "BAC": 0.27},
    },
    {
        "id": "X8-000",
        "scoring": "value_json",
        "answer": 0.47,
        "tol": 0.03,
        "values": {"0": 0.1, "3": 0.47, "4": 0.45},
    },
]

WRONG = {
    "X1-000": {"answer": "loss"},
    "X1-001": {"answer": [0, 1, 2]},
    "X2-000": {"answer": ["B", "D"]},
    "X3-000": {"win": 0.0, "tie": 0.0, "loss": 1.0},
    "X4-000": {"answer": 50},
    "X5-000": {"answer": 0.1},
    "X6-000": {"answer": []},
    "X7-000": {"answer": "ABC"},
    "X8-000": {"answer": 0},
}


class ScoringTests(unittest.TestCase):
    def test_oracle_answer_is_correct_for_every_rule(self):
        for label in LABELS:
            with self.subTest(label["id"]):
                correct, graded = score.score_item(label, oracle_answer(label))
                self.assertEqual(correct, 1)
                self.assertAlmostEqual(graded, 1.0)

    def test_wrong_answers_are_rejected(self):
        for label in LABELS:
            with self.subTest(label["id"]):
                self.assertEqual(score.score_item(label, WRONG[label["id"]])[0], 0)

    def test_options_within_tolerance_are_correct(self):
        label = next(item for item in LABELS if item["id"] == "X8-000")
        self.assertEqual(score.score_item(label, {"answer": 4})[0], 1)

    def test_answer_types_are_not_coerced(self):
        label = next(item for item in LABELS if item["id"] == "X8-000")
        self.assertEqual(score.score_item(label, {"answer": "3"}), (0, 0.0))

    def test_parse(self):
        self.assertEqual(
            score.parse('```json\n{"answer": "win"}\n```'), {"answer": "win"}
        )
        self.assertEqual(score.parse('The answer is {"answer": 3}.'), {"answer": 3})
        self.assertIsNone(score.parse("I think B."))
        self.assertIsNone(score.parse("{not json}"))
        self.assertEqual(score.score_item(LABELS[0], None), (0, 0.0))


class AdapterTests(unittest.TestCase):
    def test_sanitize_task_id(self):
        self.assertEqual(sanitize_task_id("R1-000"), "r1-000")
        self.assertEqual(sanitize_task_id("N2 / 026"), "n2-026")
        with self.assertRaises(ValueError):
            sanitize_task_id("--")

    def test_write_task(self):
        prompt = {
            "id": "X2-000",
            "system": "You are an expert player. Answer with a single JSON object.",
            "prompt": '## Question\nWhich actions are legal?\n\n## Answer format\n{"answer": [letters]} {prompt} {task_name}',
            "answer_format": '{"answer": [letters]}',
            "scenario": "X2",
            "category": "rules",
            "max_output_tokens": 80,
        }
        label = LABELS[2]
        task = TavernBenchTask(prompt=prompt, label=label)

        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp) / task.task_id
            TavernBenchAdapter(Path(tmp))._write_task(task, task_dir)

            for relative in (
                "task.toml",
                "instruction.md",
                "environment/Dockerfile",
                "solution/solve.sh",
                "tests/test.sh",
                "tests/score.py",
                "tests/label.json",
            ):
                self.assertTrue((task_dir / relative).is_file(), relative)

            toml_text = (task_dir / "task.toml").read_text()
            self.assertIn('name = "mcg-nju/tavernbench__x2-000"', toml_text)
            self.assertIn('scenario = "X2"', toml_text)
            self.assertNotIn("{task_name}", toml_text)

            instruction = (task_dir / "instruction.md").read_text()
            self.assertTrue(instruction.startswith(prompt["system"]))
            # Braces and placeholder-like text inside the prompt survive untouched.
            self.assertIn(prompt["prompt"], instruction)
            self.assertIn("/app/answer.json", instruction)
            # The label never appears in files the agent can see.
            self.assertNotIn('"B", "D", "F"', instruction)

            solve = (task_dir / "solution" / "solve.sh").read_text()
            answer_line = solve.split("<<'TAVERNBENCH_ANSWER'\n")[1].split(
                "\nTAVERNBENCH_ANSWER"
            )[0]
            self.assertEqual(score.score_item(label, json.loads(answer_line))[0], 1)

            self.assertEqual(
                json.loads((task_dir / "tests" / "label.json").read_text()), label
            )


if __name__ == "__main__":
    unittest.main()
