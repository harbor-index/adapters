"""Source-fidelity checks; set PLANBENCH_SOURCE to the pinned checkout."""

import ast
import json
import os
import tempfile
import unittest
from pathlib import Path

from blocksworld_hard.adapter import PROMPT, BlocksworldHardAdapter


@unittest.skipUnless(os.environ.get("PLANBENCH_SOURCE"), "Set PLANBENCH_SOURCE")
class SourceFidelityTests(unittest.TestCase):
    def test_full_release_and_prompt(self):
        source = Path(os.environ["PLANBENCH_SOURCE"])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            BlocksworldHardAdapter(output, source).run()
            self.assertEqual(len(list(output.iterdir())), 110)
            upstream = source / "llm_planning_analysis"
            module = ast.parse((upstream / "back_prompting_parallel.py").read_text())
            prompts = [
                node.value
                for node in ast.walk(module)
                if isinstance(node, ast.Constant) and node.value == PROMPT
            ]
            self.assertEqual(len(prompts), 1)
            references = json.loads(
                (upstream / "instances/blocksworld_hard/info.json").read_text()
            )
            for number in range(1, 111):
                task = output / f"blocksworld-hard-{number:03d}"
                problem = (
                    upstream
                    / f"instances/blocksworld_hard/generated/instance-{number}.pddl"
                )
                self.assertEqual(
                    (task / "tests/problem.pddl").read_bytes(), problem.read_bytes()
                )
                self.assertEqual(
                    (task / "solution/plan.txt").read_text().strip(),
                    references[f"instance-{number}.pddl"]["plan"].strip(),
                )
                self.assertEqual(
                    [p.name for p in (task / "environment").iterdir()], ["Dockerfile"]
                )

    def test_selective_overwrite_preserves_other_tasks(self):
        source = Path(os.environ["PLANBENCH_SOURCE"])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            BlocksworldHardAdapter(output, source, limit=2).run()
            sentinel = output / "blocksworld-hard-002/sentinel"
            sentinel.write_text("keep")
            BlocksworldHardAdapter(
                output, source, task_ids=["blocksworld-hard-001"], overwrite=True
            ).run()
            self.assertEqual(sentinel.read_text(), "keep")
            with self.assertRaises(ValueError):
                BlocksworldHardAdapter(output, source, task_ids=["missing"]).run()


if __name__ == "__main__":
    unittest.main()
