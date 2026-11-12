"""Verify conversion against the actual pinned public release, without model calls."""

import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import tomllib
import unittest
from unittest.mock import patch

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
        self.assertEqual(len(task_names), len(self.manifest["problems"]))
        self.assertEqual(input_count, sum(len(p["data_files"]) for p in self.manifest["problems"]))

    def test_image_context_contains_only_tools_and_permitted_inputs(self):
        for problem in self.manifest["problems"]:
            config = json.loads((self.source / problem["eval_config"]).read_text())
            environment = self.output / problem["eval_id"] / "environment"
            actual = {p.relative_to(environment).as_posix() for p in environment.rglob("*") if p.is_file()}
            expected = {"Dockerfile", "requirements.in", "requirements.txt", "LICENSE.dataset", *config["data_files"]}
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

    def _copy_source(self):
        source = Path(self.temporary.name) / self.id().split(".")[-1]
        shutil.copytree(self.source, source)
        return source, json.loads((source / "manifest.json").read_text())

    @staticmethod
    def _write_json(path, value):
        path.chmod(0o644)
        path.write_text(json.dumps(value, indent=2) + "\n")

    def _seal_source(self, source, manifest):
        """Publish fixture changes using the upstream manifest/checksum contract."""
        for problem in manifest["problems"]:
            for entry in problem["files"]:
                path = source / entry["path"]
                if path.is_file():
                    data = path.read_bytes()
                    entry["sha256"] = hashlib.sha256(data).hexdigest()
                    entry["bytes"] = len(data)
        self._write_json(source / "manifest.json", manifest)
        digest = hashlib.sha256((source / "manifest.json").read_bytes()).hexdigest()
        checksums = source / "checksums.sha256"
        lines = checksums.read_text().splitlines()
        checksums.chmod(0o644)
        checksums.write_text("\n".join(
            f"{digest}  manifest.json" if line.split(maxsplit=1)[1] == "manifest.json" else line
            for line in lines
        ) + "\n")

    def _add_problem(self, source, manifest, upstream_id):
        problem = json.loads(json.dumps(manifest["problems"][0]))
        old_directory = problem["problem_dir"]
        new_directory = "problems/future_case"
        shutil.copytree(source / old_directory, source / new_directory)
        problem.update(eval_id=upstream_id, release_order=len(manifest["problems"]), problem_dir=new_directory)
        problem["eval_config"] = problem["eval_config"].replace(old_directory, new_directory)
        problem["data_files"] = [p.replace(old_directory, new_directory) for p in problem["data_files"]]
        for entry in problem["files"]:
            entry["path"] = entry["path"].replace(old_directory, new_directory)
        config_path = source / problem["eval_config"]
        config = json.loads(config_path.read_text())
        config["id"] = upstream_id
        config["task"] += "\nPreserve literal {task}, {input_files}, and {source_revision}."
        self._write_json(config_path, config)
        manifest["problems"].append(problem)
        manifest["problem_count"] = len(manifest["problems"])
        return config

    def test_new_revision_and_task_need_no_code_changes(self):
        source, manifest = self._copy_source()
        upstream_id = 'Future/Case "{source_revision}"'
        config = self._add_problem(source, manifest, upstream_id)
        self._seal_source(source, manifest)
        output = source.parent / "future-output"
        revision = "1" * 40
        with patch("genebench_pro.adapter.snapshot_download", return_value=str(source)) as download:
            GeneBenchProAdapter(output, revision=revision).run()
        self.assertEqual(download.call_args.kwargs["revision"], revision)
        self.assertEqual(download.call_args.kwargs["repo_id"], DATASET_ID)
        tasks = list(output.iterdir())
        self.assertEqual(len(tasks), len(manifest["problems"]))
        names = set()
        for task in tasks:
            metadata = tomllib.loads((task / "task.toml").read_text())
            name = metadata["task"]["name"]
            self.assertRegex(name, r"^openai/[a-z0-9]+(?:-[a-z0-9]+)*$")
            names.add(name)
            self.assertEqual(metadata["metadata"]["source_revision"], revision)
            if metadata["metadata"]["upstream_id"] == upstream_id:
                self.assertTrue((task / "instruction.md").read_text().startswith(config["task"] + "\n"))
                submission = json.loads((task / "solution/eval_answer.json").read_text())
                self.assertTrue(self.grader.evaluate(config, submission)["passed"])
        self.assertEqual(len(names), len(tasks))
        self.assertTrue((output / "future-case-source-revision").is_dir())

    def test_colliding_task_names_are_rejected_before_writing(self):
        source, manifest = self._copy_source()
        self._add_problem(source, manifest, manifest["problems"][0]["eval_id"].replace("_", "-"))
        self._seal_source(source, manifest)
        output = source.parent / "collision-output"
        with patch("genebench_pro.adapter.snapshot_download", return_value=str(source)):
            with self.assertRaisesRegex(ValueError, "Duplicate registry task name"):
                GeneBenchProAdapter(output).run()
        self.assertFalse(output.exists())

    def test_missing_checksum_coverage_is_rejected(self):
        source, manifest = self._copy_source()
        problem = manifest["problems"][0]
        missing = problem["data_files"][0]
        problem["files"] = [entry for entry in problem["files"] if entry["path"] != missing]
        self._seal_source(source, manifest)
        with self.assertRaisesRegex(ValueError, "Missing upstream checksums"):
            GeneBenchProAdapter._verify_checksums(source)
        checksums = source / "checksums.sha256"
        checksums.write_text("\n".join(
            line for line in checksums.read_text().splitlines()
            if line.split(maxsplit=1)[1] != "reference_grader.py"
        ) + "\n")
        with self.assertRaisesRegex(ValueError, "Missing upstream checksum: reference_grader.py"):
            GeneBenchProAdapter._verify_checksums(source)

    def test_unsafe_and_unlisted_inputs_are_rejected_before_writing(self):
        source, manifest = self._copy_source()
        config_path = source / manifest["problems"][0]["eval_config"]
        config = json.loads(config_path.read_text())
        output = source.parent / "unsafe-output"
        for relative in ("../escape", "/tmp/escape", "data_files/../../escape", "data_files/unlisted.txt"):
            with self.subTest(path=relative):
                config["data_files"] = [relative]
                self._write_json(config_path, config)
                self._seal_source(source, manifest)
                with patch("genebench_pro.adapter.snapshot_download", return_value=str(source)):
                    with self.assertRaises(ValueError):
                        GeneBenchProAdapter(output, overwrite=True).run()
                self.assertFalse(output.exists())

    def test_failed_overwrite_preserves_existing_task(self):
        output = Path(self.temporary.name) / "failed-overwrite-output"
        task_id = "wf_selection"
        adapter = GeneBenchProAdapter(output, task_ids=[task_id], overwrite=True, cache_dir=self.cache, offline=True)
        adapter.run()
        task = output / task_id
        marker = task / "preserve-on-failure"
        marker.write_text("keep")
        with patch.object(adapter, "_write_task", side_effect=OSError("copy failed")):
            with self.assertRaisesRegex(OSError, "copy failed"):
                adapter.run()
        self.assertEqual(marker.read_text(), "keep")
        original_replace = Path.replace
        def fail_install(path, target):
            if path.name == "task":
                raise OSError("install failed")
            return original_replace(path, target)
        with patch.object(Path, "replace", fail_install):
            with self.assertRaisesRegex(OSError, "install failed"):
                adapter.run()
        self.assertEqual(marker.read_text(), "keep")
        self.assertEqual([p.name for p in output.iterdir()], [task_id])

    def test_symlink_destination_is_not_overwritten(self):
        output = Path(self.temporary.name) / "symlink-output"
        output.mkdir()
        target = output.parent / "outside-task"
        target.mkdir()
        marker = target / "keep"
        marker.write_text("keep")
        (output / "wf_selection").symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            GeneBenchProAdapter(output, task_ids=["wf_selection"], overwrite=True, cache_dir=self.cache, offline=True).run()
        self.assertEqual(marker.read_text(), "keep")

    def test_revision_must_be_an_immutable_commit(self):
        for revision in ("main", "latest", "abc123"):
            with self.subTest(revision=revision), self.assertRaisesRegex(ValueError, "full lowercase"):
                GeneBenchProAdapter(Path("unused"), revision=revision)


if __name__ == "__main__":
    unittest.main()
