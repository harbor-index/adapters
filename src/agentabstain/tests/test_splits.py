from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agentabstain import adapter
from agentabstain.adapter import AgentAbstainAdapter, SourceTask


class ParitySplitTests(unittest.TestCase):
    def test_frozen_split_keeps_both_variants_and_rejects_an_incomplete_snapshot(self):
        manifest = Path(adapter.__file__).with_name("parity_pairs.txt")
        pairs = manifest.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(set(pairs)), 16)
        rows = []
        for pair in ["ambiguous_action_specification/preview_001", *pairs]:
            category, task_id = pair.split("/")
            for variant in ("act", "abstain"):
                rows.append(SourceTask(category, task_id, variant, pair, {}, {}, {}))
        with TemporaryDirectory() as output:
            generator = AgentAbstainAdapter(Path(output), split="parity")
            self.assertEqual(generator._select_split(rows), rows[2:])
            with self.assertRaisesRegex(ValueError, pairs[-1]):
                generator._select_split(rows[:-2])
            previous = Path(output) / "old-development-task" / "task.toml"
            previous.parent.mkdir()
            previous.write_text("previous task")
            generator.overwrite = True
            with patch.object(generator, "_load_tasks", return_value=rows):
                with self.assertRaisesRegex(ValueError, "fresh output directory"):
                    generator.run()
            self.assertEqual(list(Path(output).iterdir()), [previous.parent])
            self.assertEqual(previous.read_text(), "previous task")
