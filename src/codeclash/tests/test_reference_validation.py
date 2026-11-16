"""Reference runs must not turn stale evidence into a new successful run."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_native_references.py"


class ReferenceValidationTests(unittest.TestCase):
    def invoke(self, root: Path, arenas: list[dict]) -> subprocess.CompletedProcess:
        catalog = root / "catalog.json"
        catalog.write_text(json.dumps({"arenas": arenas}))
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--upstream-repo",
                str(root / "unused-upstream"),
                "--catalog",
                str(catalog),
                "--plan",
                str(root / "unused-plan.json"),
                "--output-dir",
                str(root / "output"),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_existing_pass_reports_cannot_be_reused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            attempt = root / "output/ants/attempt-1"
            attempt.mkdir(parents=True)
            report = attempt / "report.json"
            original = '{"status":"passed","sentinel":"earlier-run"}\n'
            report.write_text(original)
            summary = root / "output/summary.json"
            summary.write_text(original)
            result = self.invoke(root, [{"id": "ants"}])
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Refusing existing attempt directories", result.stderr)
            self.assertEqual(report.read_text(), original)
            self.assertEqual(summary.read_text(), original)
            self.assertFalse((root / "output/ants.log").exists())
            self.assertFalse((root / "output/source-hashes-before.json").exists())

    def test_empty_catalog_cannot_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self.invoke(root, [])
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("at least one arena", result.stderr)
            self.assertFalse((root / "output").exists())


if __name__ == "__main__":
    unittest.main()
