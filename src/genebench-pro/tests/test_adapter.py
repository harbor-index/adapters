"""Verify conversion against the actual pinned public release, without model calls."""

import hashlib
import importlib.util
import json
import shutil
import tempfile
import tomllib
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from huggingface_hub import snapshot_download

from genebench_pro.adapter import (
    DATASET_ID,
    DATASET_REVISION,
    DOWNLOAD_PATTERNS,
    GeneBenchProAdapter,
)


class AdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        repo_root = Path(__file__).resolve().parents[3]
        cls.cache = repo_root / "tmp" / "genebench-pro" / "huggingface"
        cls.source = Path(
            snapshot_download(
                DATASET_ID,
                repo_type="dataset",
                revision=DATASET_REVISION,
                cache_dir=cls.cache,
                local_files_only=True,
                token=False,
                allow_patterns=DOWNLOAD_PATTERNS,
            )
        )
        cls.manifest = json.loads((cls.source / "manifest.json").read_text())
        cls.temporary = tempfile.TemporaryDirectory(prefix="genebench-pro-tests-")
        cls.output = Path(cls.temporary.name) / "tasks"
        GeneBenchProAdapter(cls.output, cache_dir=cls.cache, offline=True).run()
        spec = importlib.util.spec_from_file_location(
            "upstream_grader", cls.source / "reference_grader.py"
        )
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
                instruction = (task / "instruction.md").read_text()
                self.assertTrue(instruction.startswith(config["task"] + "\n"))
                self.assertEqual(
                    config_path.read_bytes(),
                    (task / "tests/eval_config.json").read_bytes(),
                )
                self.assertEqual(
                    (self.source / "reference_grader.py").read_bytes(),
                    (task / "tests/reference_grader.py").read_bytes(),
                )
                for relative in config["data_files"]:
                    source_file = config_path.parent / relative
                    task_file = task / "environment" / relative
                    self.assertEqual(source_file.read_bytes(), task_file.read_bytes())
                    input_count += 1
                metadata = tomllib.loads((task / "task.toml").read_text())
                task_names.add(metadata["task"]["name"])
        self.assertEqual(len(task_names), len(self.manifest["problems"]))
        expected_inputs = sum(
            len(problem["data_files"]) for problem in self.manifest["problems"]
        )
        self.assertEqual(input_count, expected_inputs)

    def test_image_context_contains_only_tools_and_permitted_inputs(self):
        for problem in self.manifest["problems"]:
            config = json.loads((self.source / problem["eval_config"]).read_text())
            environment = self.output / problem["eval_id"] / "environment"
            actual = {
                path.relative_to(environment).as_posix()
                for path in environment.rglob("*")
                if path.is_file()
            }
            expected = {
                "Dockerfile",
                "requirements.in",
                "requirements.txt",
                "LICENSE.dataset",
                *config["data_files"],
            }
            self.assertEqual(actual, expected)

    def test_all_oracle_answers_pass_the_original_grader(self):
        for problem in self.manifest["problems"]:
            task = self.output / problem["eval_id"]
            config = json.loads((task / "tests/eval_config.json").read_text())
            answer = json.loads((task / "solution/eval_answer.json").read_text())
            result = self.grader.evaluate(config, answer)
            self.assertTrue(result["passed"], problem["eval_id"])

    def test_task_selection_limit_and_overwrite(self):
        output = Path(self.temporary.name) / "selection"
        task_id = "wf_selection"
        adapter = GeneBenchProAdapter(
            output, task_ids=[task_id], limit=1, cache_dir=self.cache, offline=True
        )
        adapter.run()
        self.assertEqual([p.name for p in output.iterdir()], [task_id])
        marker = output / task_id / "old-generated-file"
        marker.write_text("old")
        adapter.run()
        self.assertTrue(marker.exists())
        GeneBenchProAdapter(
            output, task_ids=[task_id], overwrite=True,
            cache_dir=self.cache, offline=True,
        ).run()
        self.assertFalse(marker.exists())
        with self.assertRaisesRegex(ValueError, "Unknown task IDs"):
            GeneBenchProAdapter(
                output, task_ids=["unknown"], cache_dir=self.cache, offline=True
            ).run()

    def test_changed_scientific_input_is_rejected(self):
        damaged = Path(self.temporary.name) / "damaged-source"
        shutil.copytree(self.source, damaged)
        relative = self.manifest["problems"][0]["data_files"][0]
        path = damaged / relative
        path.chmod(0o644)
        path.write_bytes(path.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, relative):
            self._run_with_source(damaged, damaged.parent / "damaged-output")

    @staticmethod
    def _run_with_source(source, output):
        with patch("genebench_pro.adapter.snapshot_download", return_value=str(source)):
            GeneBenchProAdapter(output).run()

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
        checksums = source / "checksums.sha256"
        checksums.chmod(0o644)
        lines = []
        for path in sorted(source.rglob("*")):
            if path.is_file() and path != checksums:
                checksum = hashlib.sha256(path.read_bytes()).hexdigest()
                lines.append(f"{checksum}  {path.relative_to(source).as_posix()}")
        checksums.write_text("\n".join(lines) + "\n")

    def _add_problem(self, source, manifest, upstream_id):
        problem = deepcopy(manifest["problems"][0])
        old_directory = problem["problem_dir"]
        new_directory = "problems/future_case"
        shutil.copytree(source / old_directory, source / new_directory)
        problem.update(
            eval_id=upstream_id,
            release_order=len(manifest["problems"]),
            problem_dir=new_directory,
        )
        problem["eval_config"] = problem["eval_config"].replace(
            old_directory, new_directory
        )
        problem["data_files"] = [
            path.replace(old_directory, new_directory) for path in problem["data_files"]
        ]
        for entry in problem["files"]:
            entry["path"] = entry["path"].replace(old_directory, new_directory)
        config_path = source / problem["eval_config"]
        config = json.loads(config_path.read_text())
        config["id"] = upstream_id
        config["task"] += (
            "\nPreserve literal {task}, {input_files}, and {source_revision}."
        )
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
        with patch(
            "genebench_pro.adapter.snapshot_download", return_value=str(source)
        ) as download:
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
                instruction = (task / "instruction.md").read_text()
                self.assertTrue(instruction.startswith(config["task"] + "\n"))
                answer_path = task / "solution/eval_answer.json"
                submission = json.loads(answer_path.read_text())
                self.assertTrue(self.grader.evaluate(config, submission)["passed"])
        self.assertEqual(len(names), len(tasks))
        self.assertTrue((output / "future-case-source-revision").is_dir())

    def test_colliding_task_names_are_rejected_before_writing(self):
        source, manifest = self._copy_source()
        colliding_id = manifest["problems"][0]["eval_id"].replace("_", "-")
        self._add_problem(source, manifest, colliding_id)
        self._seal_source(source, manifest)
        output = source.parent / "collision-output"
        with self.assertRaisesRegex(ValueError, "Duplicate registry task name"):
            self._run_with_source(source, output)
        self.assertFalse(output.exists())

    def test_missing_checksum_coverage_is_rejected(self):
        source, manifest = self._copy_source()
        output = source.parent / "missing-checksum-output"
        checksums = source / "checksums.sha256"
        checksums.chmod(0o644)
        missing_files = [
            manifest["problems"][0]["data_files"][0],
            "reference_grader.py",
        ]
        for missing in missing_files:
            lines = [
                line
                for line in checksums.read_text().splitlines()
                if line.split(maxsplit=1)[1] != missing
            ]
            checksums.write_text("\n".join(lines) + "\n")
            with self.assertRaisesRegex(KeyError, missing):
                self._run_with_source(source, output)

    def test_unsafe_and_unlisted_inputs_are_rejected_before_writing(self):
        source, manifest = self._copy_source()
        config_path = source / manifest["problems"][0]["eval_config"]
        config = json.loads(config_path.read_text())
        output = source.parent / "unsafe-output"
        invalid_paths = {
            ".": ValueError,
            "../escape": ValueError,
            "/tmp/escape": ValueError,
            "data_files/../../escape": ValueError,
            "data_files/unlisted.txt": KeyError,
        }
        for relative, error in invalid_paths.items():
            with self.subTest(path=relative):
                config["data_files"] = [relative]
                self._write_json(config_path, config)
                self._seal_source(source, manifest)
                with self.assertRaises(error):
                    self._run_with_source(source, output)
                self.assertEqual(list(output.iterdir()), [])

    def test_failed_preparation_preserves_existing_task(self):
        output = Path(self.temporary.name) / "failed-overwrite-output"
        task_id = "wf_selection"
        adapter = GeneBenchProAdapter(
            output, task_ids=[task_id], overwrite=True,
            cache_dir=self.cache, offline=True,
        )
        adapter.run()
        task = output / task_id
        marker = task / "preserve-on-failure"
        marker.write_text("keep")
        with patch.object(adapter, "_write_task", side_effect=OSError("copy failed")):
            with self.assertRaisesRegex(OSError, "copy failed"):
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
            GeneBenchProAdapter(
                output, task_ids=["wf_selection"], overwrite=True,
                cache_dir=self.cache, offline=True,
            ).run()
        self.assertEqual(marker.read_text(), "keep")

    def test_revision_must_be_an_immutable_commit(self):
        for revision in ("main", "latest", "abc123"):
            with self.subTest(revision=revision):
                with self.assertRaisesRegex(ValueError, "full lowercase"):
                    GeneBenchProAdapter(Path("unused"), revision=revision)


if __name__ == "__main__":
    unittest.main()
