from concurrent.futures import ThreadPoolExecutor
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event
import unittest

import yaml
from unittest.mock import patch

from loopsbench.adapter import LoopsBenchAdapter
from loopsbench.main import PARITY_TASK_IDS
from loopsbench.parity import check_version, prepare, SMOKE_IDS


class GenerationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.output = self.root / "output"

    def make_task(self, task_id="distributed-fs-chfs"):
        task = self.source / ("task_" + task_id.replace("-", "_"))
        task.mkdir()
        (task / "task.yaml").write_text("instruction: Original\n", encoding="utf-8")
        (task / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        for name in ("run-tests.sh", "solution.sh"):
            (task / name).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        return task

    def adapter(self, **kwargs):
        return LoopsBenchAdapter(self.source, self.output, **kwargs)

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "loopsbench.main", "--tasks-dir", str(self.source),
             "--output-dir", str(self.output), *args],
            capture_output=True, text=True, check=False,
        )

    def test_failed_new_generation_leaves_no_task_and_can_be_retried(self):
        self.make_task()
        adapter = self.adapter()
        generate = adapter.generate_task

        def fail_after_generation(src, staged):
            generate(src, staged)
            self.assertTrue((staged / "task.toml").is_file())
            self.assertEqual(list(self.output.glob("*/task.toml")), [])
            raise OSError("generation interrupted")

        with patch.object(adapter, "generate_task", side_effect=fail_after_generation):
            with self.assertRaisesRegex(OSError, "generation interrupted"):
                adapter.run()
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual(adapter.run(), [self.output / "distributed-fs-chfs"])

    def test_legacy_complete_task_is_kept_and_failed_overwrite_preserves_it(self):
        source = self.make_task()
        (task,) = self.adapter().run()
        config = task / "task.toml"
        legacy_config = config.read_text(encoding="utf-8").replace(
            'environment_mode = "separate"', 'environment_mode = "shared"',
        )
        config.write_text(legacy_config, encoding="utf-8")
        verifier = task / "tests/test.sh"
        verifier.write_text(
            "".join(line for line in verifier.read_text().splitlines(keepends=True)
                    if "workspace_snapshot.py" not in line),
            encoding="utf-8",
        )
        (task / "tests/_loopsbench/workspace_snapshot.py").unlink()
        marker = task / "keep-on-failure"
        marker.touch()
        (source / "task.yaml").write_text("instruction: Updated\n", encoding="utf-8")

        self.assertEqual(self.adapter().run(), [])
        self.assertTrue(marker.is_file())
        overwrite = self.adapter(overwrite=True)

        def fail_to_publish(staged, destination):
            self.assertEqual(destination, task)
            self.assertTrue(marker.is_file())
            self.assertEqual((staged / "instruction.md").read_text(), "Updated\n")
            raise OSError("publish failed")

        with patch("loopsbench.adapter._exchange_task_dirs", side_effect=fail_to_publish):
            with self.assertRaisesRegex(OSError, "publish failed"):
                overwrite.run()
        self.assertEqual(list(self.output.iterdir()), [task])
        self.assertTrue(marker.is_file())
        self.assertEqual(config.read_text(encoding="utf-8"), legacy_config)
        self.assertEqual((task / "instruction.md").read_text(), "Original\n")

        self.assertEqual(overwrite.run(), [task])
        self.assertEqual(list(self.output.iterdir()), [task])
        self.assertFalse(marker.exists())
        self.assertEqual((task / "instruction.md").read_text(), "Updated\n")

    def test_incomplete_existing_tasks_are_retried_without_overwrite(self):
        self.make_task()
        task = self.output / "distributed-fs-chfs"
        task.mkdir(parents=True)
        (task / "instruction.md").touch()
        self.assertEqual(self.adapter().run(), [task])

        (task / "task.toml").write_text("[task\n", encoding="utf-8")
        self.assertEqual(self.adapter().run(), [task])

        (task / "tests/_loopsbench/workspace_snapshot.py").unlink()
        self.assertEqual(self.adapter().run(), [task])
        self.assertEqual(self.adapter().run(), [])

    def test_generated_task_must_be_complete_before_publishing(self):
        self.make_task()
        adapter = self.adapter()
        with patch.object(adapter, "_write_task_toml"):
            with self.assertRaisesRegex(ValueError, "produced an incomplete task"):
                adapter.run()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_colliding_selected_ids_fail_before_writing(self):
        self.make_task("architecture-riscv-labs")
        self.make_task()
        duplicate = self.source / "distributed-fs-chfs"
        duplicate.mkdir()
        (duplicate / "task.yaml").touch()
        result = self.cli("--task-ids", "architecture-riscv-labs", "distributed-fs-chfs")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("both map to Harbor id 'distributed-fs-chfs'", result.stderr)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_repair_rejects_output_that_would_replace_its_input(self):
        source = self.make_task().rename(self.source / "distributed-fs-chfs")
        with self.assertRaisesRegex(ValueError, "overlaps its source directory"):
            LoopsBenchAdapter(self.source, self.source).run()
        self.assertEqual((source / "task.yaml").read_text(), "instruction: Original\n")

    def test_concurrent_generators_skip_the_task_published_by_the_first(self):
        self.make_task()
        first, second = self.adapter(), self.adapter()
        generate = first.generate_task
        staged = Event()
        publish = Event()
        second_started = Event()

        def wait_to_publish(src, dst):
            generate(src, dst)
            staged.set()
            if not publish.wait(5):
                raise TimeoutError("publication did not resume")

        def run_second():
            second_started.set()
            return second.run()

        with (patch.object(first, "generate_task", side_effect=wait_to_publish),
              patch.object(second, "generate_task", wraps=second.generate_task) as second_generate,
              ThreadPoolExecutor(max_workers=2) as executor):
            initial = executor.submit(first.run)
            try:
                self.assertTrue(staged.wait(5))
                output_fd = os.open(self.output, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(output_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(output_fd)
                concurrent = executor.submit(run_second)
                self.assertTrue(second_started.wait(5))
                self.assertEqual(list(self.output.glob("*/task.toml")), [])
            finally:
                publish.set()
            self.assertEqual(initial.result(timeout=5), [self.output / "distributed-fs-chfs"])
            self.assertEqual(concurrent.result(timeout=5), [])
            second_generate.assert_not_called()

    @patch("loopsbench.parity.native_prompts", side_effect=lambda checkout, dirs: ["final prompt"] * len(dirs))
    def test_paired_plans_keep_exact_task_sets_and_endpoint_access(self, render):
        for task_id in PARITY_TASK_IDS:
            src = self.make_task(task_id)
            if task_id == "numpy-dl-aics-style":
                (src / "docker-compose.yaml").write_text(yaml.safe_dump({"services": {
                    "client": {"network_mode": "host"}, "tester": {"image": "task-client"}
                }}))
        self.adapter(bundle="c47e13a").run()
        for split, expected in [("smoke", SMOKE_IDS), ("full", PARITY_TASK_IDS)]:
            plan = self.root / split
            prepare(self.source, self.output, plan, split=split, model="openai/test",
                    base_url="https://model.example/v1", version="1.2.3",
                    binary=Path("/opt/test/codex"), checkout=self.root)
            job = yaml.safe_load((plan / "harbor.yaml").read_text())
            self.assertEqual([Path(t["path"]).name for t in job["tasks"]], list(expected))
            self.assertEqual(job["agents"][0]["kwargs"]["version"], "1.2.3")
            self.assertEqual(job["agents"][0]["extra_allowed_hosts"], ["model.example"])
            for task_id in expected:
                self.assertEqual((plan / "harbor-tasks" / task_id / "instruction.md").read_bytes(), b"final prompt")
                native = yaml.safe_load((plan / task_id / "run.yaml").read_text())
                self.assertEqual(native["agent"], "codex")
                self.assertEqual(native["model"], job["agents"][0]["model_name"])
                self.assertEqual(native["n_attempts"], 1)
                self.assertEqual(native["agent_kwargs"]["outer_loop_count"], 1)
                self.assertEqual(native["network"]["agent"], {
                    "network_mode": "allowlist", "allowed_hosts": ["model.example"]
                })
                source_id = "task_" + task_id.replace("-", "_")
                self.assertEqual(native["task_ids"], [source_id])
                compose = Path(native["dataset_path"]) / source_id / "docker-compose.yaml"
                self.assertNotIn("network_mode", yaml.safe_load(compose.read_text())["services"]["client"])

    def test_version_check_reads_both_pins_from_plan(self):
        executable = self.root / "codex"
        executable.write_bytes(b"\x7fELF")
        plan = self.root
        (plan / "one").mkdir()
        (plan / "one/run.yaml").write_text(yaml.safe_dump({
            "agent_kwargs": {"codex_binary": str(executable)}}))
        (plan / "harbor.yaml").write_text(yaml.safe_dump({
            "tasks": [{"path": "harbor-tasks/one"}], "agents": [{"kwargs": {"version": "1.2.3"}}]}))
        with patch("loopsbench.parity.subprocess.check_output", return_value="codex-cli 1.2.4") as run:
            with self.assertRaisesRegex(ValueError, "requires 1.2.3"):
                check_version(plan)
            run.assert_called_once_with([executable, "--version"], text=True)
            run.return_value = "codex-cli 1.2.3"
            check_version(plan)

    def test_parity_cli_checks_all_ten_tasks_including_skips(self):
        for task_id in PARITY_TASK_IDS:
            self.make_task(task_id)
        initial = self.cli("--split", "parity")
        self.assertEqual(initial.returncode, 0, initial.stderr)
        self.assertIn("10 complete tasks", initial.stderr)
        self.assertIn("(10 generated)", initial.stderr)
        self.assertEqual({p.name for p in self.output.iterdir()}, set(PARITY_TASK_IDS))

        unchanged = self.output / PARITY_TASK_IDS[0] / "task.toml"
        original_inode = unchanged.stat().st_ino
        incomplete = self.output / PARITY_TASK_IDS[1] / "environment/Dockerfile"
        incomplete.unlink()
        retry = self.cli("--split", "parity")
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertIn("10 complete tasks", retry.stderr)
        self.assertIn("(1 generated)", retry.stderr)
        self.assertTrue(incomplete.is_file())
        self.assertEqual(unchanged.stat().st_ino, original_inode)

        rerun = self.cli("--split", "parity")
        self.assertEqual(rerun.returncode, 0, rerun.stderr)
        self.assertIn("10 complete tasks", rerun.stderr)
        self.assertIn("(0 generated)", rerun.stderr)

    def test_parity_cli_rejects_missing_inputs_and_extra_tasks_before_writes(self):
        for task_id in PARITY_TASK_IDS[:-1]:
            self.make_task(task_id)
        missing = self.cli("--split", "parity")
        self.assertEqual(missing.returncode, 2, missing.stderr)
        self.assertIn(PARITY_TASK_IDS[-1], missing.stderr)
        self.assertEqual(list(self.output.iterdir()), [])

        self.make_task(PARITY_TASK_IDS[-1])
        extra = self.output / "other-task"
        extra.mkdir()
        (extra / "task.toml").touch()
        mixed = self.cli("--split", "parity", "--overwrite")
        self.assertEqual(mixed.returncode, 2, mixed.stderr)
        self.assertIn("outside the parity split: ['other-task']", mixed.stderr)
        self.assertEqual(list(self.output.iterdir()), [extra])


if __name__ == "__main__":
    unittest.main()
