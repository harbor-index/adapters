import importlib.util
import io
import os
from pathlib import Path
import tarfile
import tempfile
import unittest

from loopsbench.adapter import LoopsBenchAdapter, TEMPLATE_DIR


def load_template_module(name):
    spec = importlib.util.spec_from_file_location(name, TEMPLATE_DIR / "tests" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


grader = load_template_module("loopsbench_grade")
snapshot = load_template_module("workspace_snapshot")


class GraderTests(unittest.TestCase):
    def test_normalized_aliases_keep_first_result(self):
        parsed, summary_only = grader.parse(
            "PASSED /tests/test_math.py::test_addition\n"
            "FAILED test_math.py::test_addition\n"
        )
        result = grader.merge_results(parsed, summary_only)
        self.assertEqual(result, {"test_math.py::test_addition": grader.PASSED})
        self.assertTrue(grader.is_resolved(result))

    def test_summary_is_ignored_when_case_results_exist(self):
        result = grader.merge_results(
            {"__summary__": grader.FAILED, "test_math.py::test_addition": grader.PASSED},
            summary_only=False,
        )
        self.assertTrue(grader.is_resolved(result))


class AdapterTests(unittest.TestCase):
    def test_overwrite_rejects_stale_excluded_task_before_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "inputs"
            source.mkdir()
            stale = root / "tasks" / "security-labs"
            stale.mkdir(parents=True)
            marker = stale / "task.toml"
            marker.write_text("existing task", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "contains excluded tasks"):
                LoopsBenchAdapter(source, stale.parent, overwrite=True).run()
            self.assertEqual(marker.read_text(encoding="utf-8"), "existing task")


class SnapshotTests(unittest.TestCase):
    def test_restore_preserves_build_artifacts_and_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            workspace = root / "workspace"
            build_dir = source / "build"
            build_dir.mkdir(parents=True)
            workspace.mkdir()
            (workspace / "image-only").touch()
            (source / ".gitignore").write_text("build/\n", encoding="utf-8")
            build_file = build_dir / "output"
            build_file.write_text("payload", encoding="utf-8")
            build_file.chmod(0o751)
            os.utime(str(build_file), (123456789, 123456789))
            (source / "absolute").symlink_to(workspace / "build/output")
            (build_dir / "parent").symlink_to("..")
            os.link(build_file, build_dir / "alias")
            archive_path = root / "workspace.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                archive.add(source, arcname=".")

            snapshot.restore(archive_path, workspace)
            restored = workspace / "build/output"
            self.assertEqual(restored.read_text(encoding="utf-8"), "payload")
            self.assertEqual(restored.stat().st_mtime, 123456789)
            self.assertEqual(restored.stat().st_mode & 0o777, 0o751)
            self.assertTrue((workspace / "build/alias").samefile(restored))
            self.assertEqual(os.readlink(workspace / "build/parent"), "..")
            self.assertTrue((workspace / "absolute").samefile(restored))
            self.assertFalse((workspace / "image-only").exists())

    def test_restore_preserves_an_unchanged_link_from_the_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            (workspace / "tests").mkdir(parents=True)
            link = workspace / "tests/test_runner.py"
            original_target = root / "tests/test_runner.py"
            link.symlink_to(original_target)
            archive_path = root / "workspace.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                archive.add(workspace, arcname=".")

            snapshot.restore(archive_path, workspace)
            self.assertEqual(os.readlink(link), str(original_target))

            with tarfile.open(archive_path, "w:gz") as archive:
                member = tarfile.TarInfo("tests/test_runner.py")
                member.type = tarfile.SYMTYPE
                member.linkname = str(root / "tests/alternate_runner.py")
                archive.addfile(member)

            with self.assertRaisesRegex(ValueError, "symlink leaves the workspace"):
                snapshot.restore(archive_path, workspace)
            self.assertEqual(os.readlink(link), str(original_target))

    def test_invalid_snapshot_leaves_the_existing_workspace_in_place(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            marker = workspace / "image-only"
            marker.touch()
            archive_path = root / "workspace.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                member = tarfile.TarInfo("duplicated")
                archive.addfile(member, io.BytesIO())
                archive.addfile(member, io.BytesIO())

            with self.assertRaisesRegex(ValueError, "duplicate"):
                snapshot.restore(archive_path, workspace)
            self.assertTrue(marker.is_file())
            self.assertFalse((workspace / "duplicated").exists())
