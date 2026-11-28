"""Source fidelity checks; set ACPBENCH_SOURCE to the pinned checkout."""

import gzip
import json
import os
import tempfile
import unittest
from pathlib import Path

from acpbench_hard.adapter import TEMPLATE, ACPBenchHardAdapter, oracle_answer


@unittest.skipUnless(os.environ.get("ACPBENCH_SOURCE"), "Set ACPBENCH_SOURCE")
class SourceFidelityTests(unittest.TestCase):
    def test_vendored_scorer_is_unchanged(self):
        upstream = Path(os.environ["ACPBENCH_SOURCE"]) / "src"
        for path in upstream.rglob("*"):
            if path.is_file() and path.name != "evaluation_bool_mcq.py":
                self.assertEqual(
                    path.read_bytes(),
                    (
                        TEMPLATE / "tests/upstream" / path.relative_to(upstream)
                    ).read_bytes(),
                )

    def test_parity_covers_each_source_domain_and_category(self):
        source = Path(os.environ["ACPBENCH_SOURCE"])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            ACPBenchHardAdapter(output, source, split="parity").run()
            self.assertEqual(len(list(output.iterdir())), 104)
            for category in (
                "app",
                "areach",
                "just",
                "land",
                "nexta",
                "prog",
                "reach",
                "val",
            ):
                with gzip.open(
                    source / f"dataset/{category}/test.gen.json.gz", "rt"
                ) as stream:
                    records = json.load(stream)
                tasks = list(output.glob(f"acpbench-hard-{category}-*"))
                self.assertEqual(len(tasks), 13)
                self.assertEqual(
                    {
                        json.loads((task / "tests/record.json").read_text())["id"]
                        for task in tasks
                    },
                    {record["id"] for record in records[::10]},
                )
                for task in tasks:
                    self.assertEqual(
                        [p.name for p in (task / "environment").iterdir()],
                        ["Dockerfile"],
                    )

    def test_reference_removal_respects_occurrence(self):
        record = {
            "question": 'Simplify the plan "(a x) (b y) (a x)"',
            "answer": [["(a x)", "2"]],
        }
        self.assertEqual(oracle_answer("just", record), "(a x) (b y)")


if __name__ == "__main__":
    unittest.main()
