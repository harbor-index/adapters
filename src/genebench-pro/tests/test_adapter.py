"""Verify conversion against the actual pinned public release, without model calls."""

import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import tomllib
import unittest

from huggingface_hub import snapshot_download

from genebench_pro.adapter import DATASET_ID, DATASET_REVISION, GeneBenchProAdapter


class AdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cache = Path(__file__).resolve().parents[3] / "tmp/genebench-pro/huggingface"
        cls.source = Path(snapshot_download(
            DATASET_ID, repo_type="dataset", revision=DATASET_REVISION,
            cache_dir=cls.cache, local_files_only=True,
            allow_patterns=[
                "LICENSE", "README.md", "manifest.json", "checksums.sha256",
                "reference_definitions.md", "reference_grader.py",
                "problems/*/eval_config.json", "problems/*/data_files/*",
            ],
        ))
        cls.manifest = json.loads((cls.source / "manifest.json").read_text())
        cls.temporary = tempfile.TemporaryDirectory(prefix="genebench-pro-tests-")
        cls.output = Path(cls.temporary.name) / "tasks"
        GeneBenchProAdapter(cls.output, cache_dir=cls.cache, offline=True).run()
        spec = importlib.util.spec_from_file_location("upstream_grader", cls.source / "reference_grader.py")
        cls.grader = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.grader)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_instructions_inputs_and_grader_are_preserved(self):
        task_names = set()
        input_count = 0
        for problem in self.manifest["problems"]:
            with self.subTest(problem=problem["eval_id"]):
                task = self.output / problem["eval_id"]
                config_path = self.source / problem["eval_config"]
                config = json.loads(config_path.read_text())
                self.assertTrue((task / "instruction.md").read_text().startswith(config["task"] + "\n"))
                self.assertEqual(config_path.read_bytes(), (task / "tests/eval_config.json").read_bytes())
                self.assertEqual((self.source / "reference_grader.py").read_bytes(), (task / "tests/reference_grader.py").read_bytes())
                for relative in config["data_files"]:
                    expected = hashlib.sha256((config_path.parent / relative).read_bytes()).digest()
                    actual = hashlib.sha256((task / "environment" / relative).read_bytes()).digest()
                    self.assertEqual(expected, actual)
                    input_count += 1
                metadata = tomllib.loads((task / "task.toml").read_text())
                task_names.add(metadata["task"]["name"])
        self.assertEqual(len(task_names), 10)
        self.assertEqual(input_count, 50)

    def test_image_context_contains_only_tools_and_permitted_inputs(self):
        for problem in self.manifest["problems"]:
            config = json.loads((self.source / problem["eval_config"]).read_text())
            environment = self.output / problem["eval_id"] / "environment"
            actual = {p.relative_to(environment).as_posix() for p in environment.rglob("*") if p.is_file()}
            expected = {"Dockerfile", "requirements.in", "requirements.txt", "LICENSE.genebench-pro", *config["data_files"]}
            self.assertEqual(actual, expected)

    def test_all_oracle_answers_pass_the_original_grader(self):
        for problem in self.manifest["problems"]:
            task = self.output / problem["eval_id"]
            config = json.loads((task / "tests/eval_config.json").read_text())
            answer = json.loads((task / "solution/eval_answer.json").read_text())
            self.assertTrue(self.grader.evaluate(config, answer)["passed"], problem["eval_id"])

    def test_task_selection_limit_and_overwrite(self):
        output = Path(self.temporary.name) / "selection"
        task_id = "wf_selection"
        adapter = GeneBenchProAdapter(output, task_ids=[task_id], limit=1, cache_dir=self.cache, offline=True)
        adapter.run()
        self.assertEqual([p.name for p in output.iterdir()], [task_id])
        marker = output / task_id / "old-generated-file"
        marker.write_text("old")
        adapter.run()
        self.assertTrue(marker.exists())
        GeneBenchProAdapter(output, task_ids=[task_id], overwrite=True, cache_dir=self.cache, offline=True).run()
        self.assertFalse(marker.exists())
        with self.assertRaisesRegex(ValueError, "Unknown task IDs"):
            GeneBenchProAdapter(output, task_ids=["unknown"], cache_dir=self.cache, offline=True).run()

    def test_changed_scientific_input_is_rejected(self):
        damaged = Path(self.temporary.name) / "damaged-source"
        shutil.copytree(self.source, damaged)
        relative = self.manifest["problems"][0]["data_files"][0]
        path = damaged / relative
        path.chmod(0o644)
        path.write_bytes(path.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, relative):
            GeneBenchProAdapter._verify_checksums(damaged)


if __name__ == "__main__":
    unittest.main()
