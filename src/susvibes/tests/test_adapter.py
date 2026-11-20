"""Unit tests for the SusVibes adapter and its in-container grader.

Run from ``src/susvibes/`` with ``uv run --with pytest pytest``. No Docker or
network access is needed.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import tomllib
from pathlib import Path

import pytest
from susvibes.adapter import SusVibesAdapter, new_files, parse_test_cmd

from susvibes import adapter as adapter_module

TEMPLATE_DIR = Path(adapter_module.__file__).parent / "task-template"


def _load_grader():
    spec = importlib.util.spec_from_file_location(
        "sv_eval", TEMPLATE_DIR / "tests" / "sv_eval.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sv_eval = _load_grader()

GOLDEN_PATCH = """\
diff --git a/pkg/core.py b/pkg/core.py
--- a/pkg/core.py
+++ b/pkg/core.py
@@ -1 +1,2 @@
 def f():
+    return 1
diff --git a/pkg/templates/new.html b/pkg/templates/new.html
new file mode 100644
--- /dev/null
+++ b/pkg/templates/new.html
@@ -0,0 +1 @@
+<p>{braces}</p>
"""

TEST_PATCH = """\
diff --git a/tests/test_core.py b/tests/test_core.py
--- a/tests/test_core.py
+++ b/tests/test_core.py
@@ -1 +1,2 @@
 import pkg
+def test_sec(): assert pkg.f() == 1
"""

INSTANCE_ID = "acme__widget_0123456789abcdef0123456789abcdef01234567"
IMAGE_DIGEST = "sha256:" + "ab" * 32


@pytest.fixture
def fake_upstream(tmp_path, monkeypatch):
    """A minimal upstream checkout with one instance, and matching checksums."""
    record = {
        "instance_id": INSTANCE_ID,
        "project": "acme/widget",
        "base_commit": "0123456789abcdef0123456789abcdef01234567",
        "language": "python",
        "problem_statement": "Implement `f` so that {x} is handled.\n",
        "image_name": "example/susvibes.x86_64.eval_acme_widget_0123",
        "golden_patch": GOLDEN_PATCH,
        "test_patch": TEST_PATCH,
        "expected_pf": {"func": 2, "sec": 0},
        "flags": {},
        "cwe_ids": ["CWE-79"],
        "cve_id": "CVE-2024-0001",
    }
    contents = {
        "dataset": json.dumps(record) + "\n",
        "dockerfile": json.dumps(
            {
                INSTANCE_ID: "FROM base_py:3.11\n\nWORKDIR /project\n\nCOPY . .\n\nRUN pip install -e .\n\nCMD pytest -q\n"
            }
        ),
        "logs_handler": json.dumps(
            {
                INSTANCE_ID: {
                    "count": {
                        "logs_parser": {
                            "FAILED": r"(\d+) failed",
                            "PASSED": r"(\d+) passed",
                        },
                        "logs_checker": None,
                    }
                }
            }
        ),
    }
    files = {}
    for key, (rel_path, _) in adapter_module.UPSTREAM_FILES.items():
        path = tmp_path / "upstream" / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents[key], encoding="utf-8")
        files[key] = (rel_path, hashlib.sha256(contents[key].encode()).hexdigest())
    monkeypatch.setattr(adapter_module, "UPSTREAM_FILES", files)
    digests = tmp_path / "image_digests.json"
    digests.write_text(json.dumps({INSTANCE_ID: IMAGE_DIGEST}), encoding="utf-8")
    monkeypatch.setattr(adapter_module, "IMAGE_DIGESTS_PATH", digests)
    return tmp_path / "upstream"


def test_parse_test_cmd_shell_and_exec_forms():
    assert parse_test_cmd("FROM x\nCMD pytest -q\n") == {
        "form": "shell",
        "value": "pytest -q",
    }
    assert parse_test_cmd('FROM x\nCMD ["tox", "-e", "py312"]') == {
        "form": "exec",
        "value": ["tox", "-e", "py312"],
    }
    assert (
        parse_test_cmd("FROM x\nCMD ./manage.py migrate && ./manage.py test\n")["form"]
        == "shell"
    )
    with pytest.raises(ValueError):
        parse_test_cmd("FROM x\nRUN true\n")


def test_new_files_lists_only_created_paths():
    assert new_files(GOLDEN_PATCH) == ["pkg/templates/new.html"]
    assert new_files(TEST_PATCH) == []


def test_generate_task(fake_upstream, tmp_path):
    out = tmp_path / "out"
    SusVibesAdapter(out, susvibes_root=fake_upstream).run()
    task_dir = out / INSTANCE_ID

    config = tomllib.loads((task_dir / "task.toml").read_text())
    assert config["task"]["name"] == f"leililab/susvibes__{INSTANCE_ID}"
    assert config["verifier"]["environment_mode"] == "separate"
    assert config["verifier"]["collect"][0]["command"].endswith(
        "git diff > /logs/artifacts/model.patch"
    )
    assert config["metadata"]["cwe_ids"] == ["CWE-79"]
    assert config["metadata"]["image_digest"] == IMAGE_DIGEST

    instruction = (task_dir / "instruction.md").read_text()
    assert "Implement `f` so that {x} is handled." in instruction
    assert (
        "__WORK_DIR__" not in instruction and "__PROBLEM_STATEMENT__" not in instruction
    )
    assert "<uploaded_files>\n/project\n</uploaded_files>" in instruction

    dockerfile = (task_dir / "environment" / "Dockerfile").read_text()
    assert (
        f"FROM example/susvibes.x86_64.eval_acme_widget_0123@{IMAGE_DIGEST}\n"
        in dockerfile
    )
    assert "{image_ref}" not in dockerfile

    grader_config = json.loads((task_dir / "tests" / "config.json").read_text())
    assert grader_config["test_cmd"] == {"form": "shell", "value": "pytest -q"}
    assert grader_config["expected_pf"] == {"func": 2, "sec": 0}
    assert grader_config["test_patch"] == TEST_PATCH

    assert (task_dir / "solution" / "golden.patch").read_text() == GOLDEN_PATCH
    solve = (task_dir / "solution" / "solve.sh").read_text()
    assert "git add --intent-to-add -- 'pkg/templates/new.html'" in solve
    assert "{register_new_files}" not in solve

    assert not list(task_dir.rglob("__pycache__"))
    assert [path.name for path in out.iterdir()] == [INSTANCE_ID]


def test_failed_generation_leaves_nothing_behind(fake_upstream, tmp_path, monkeypatch):
    missing = tmp_path / "no_digests.json"
    missing.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(adapter_module, "IMAGE_DIGESTS_PATH", missing)
    out = tmp_path / "out"
    with pytest.raises(SystemExit):
        SusVibesAdapter(out, susvibes_root=fake_upstream).run()
    assert list(out.iterdir()) == []


def test_packaged_image_digests_are_well_formed():
    digests = json.loads(
        (Path(adapter_module.__file__).parent / "image_digests.json").read_text()
    )
    assert len(digests) == 186
    assert all(
        len(value) == 71 and value.startswith("sha256:") for value in digests.values()
    )


def test_generate_task_refuses_existing_dir_without_overwrite(fake_upstream, tmp_path):
    out = tmp_path / "out"
    SusVibesAdapter(out, susvibes_root=fake_upstream).run()
    with pytest.raises(SystemExit):
        SusVibesAdapter(out, susvibes_root=fake_upstream).run()
    SusVibesAdapter(out, susvibes_root=fake_upstream, overwrite=True).run()


def test_task_ids_and_limit(fake_upstream, tmp_path):
    adapter = SusVibesAdapter(
        tmp_path / "out", susvibes_root=fake_upstream, task_ids=[INSTANCE_ID.upper()]
    )
    assert adapter.select_ids() == [INSTANCE_ID]
    with pytest.raises(KeyError):
        SusVibesAdapter(
            tmp_path / "out", susvibes_root=fake_upstream, task_ids=["missing"]
        ).select_ids()
    assert (
        SusVibesAdapter(
            tmp_path / "out", susvibes_root=fake_upstream, limit=0
        ).select_ids()
        == []
    )


def test_task_ids_must_be_in_the_split(fake_upstream, tmp_path, monkeypatch):
    monkeypatch.setattr(adapter_module, "PARITY_SIZE", 0)
    with pytest.raises(KeyError, match="not in the parity split"):
        SusVibesAdapter(
            tmp_path / "out",
            susvibes_root=fake_upstream,
            split="parity",
            task_ids=[INSTANCE_ID],
        ).select_ids()


def test_checksum_mismatch_is_rejected(fake_upstream, tmp_path):
    rel_path, _ = adapter_module.UPSTREAM_FILES["dataset"]
    (fake_upstream / rel_path).write_text("{}\n")
    with pytest.raises(ValueError, match="does not match SusVibes commit"):
        SusVibesAdapter(tmp_path / "out", susvibes_root=fake_upstream)


def test_grader_filters_test_files_and_binary_diffs():
    submission = (
        GOLDEN_PATCH
        + TEST_PATCH
        + (
            "diff --git a/pkg/__pycache__/core.pyc b/pkg/__pycache__/core.pyc\n"
            "Binary files a/pkg/__pycache__/core.pyc and b/pkg/__pycache__/core.pyc differ\n"
        )
    )
    filtered = sv_eval.filter_target_files(
        submission, sv_eval.touched_files(TEST_PATCH), exclude=True
    )
    filtered = sv_eval.filter_binary_files(filtered)
    assert filtered == GOLDEN_PATCH


def test_grader_counts_failures_and_thresholds():
    handler = sv_eval.LogsCount(
        logs_parser={
            "FAILED": r"(\d+) failed",
            "PASSED": r"(\d+) passed",
            "ERROR": r"(\d+) error",
        },
        logs_checker=r"^INTERNALERROR",
    )
    passed = handler.handle("===== 10 passed in 1.00s =====\n", False)
    assert (passed.status, passed.failures) == ("completed", 0)

    broken = handler.handle("===== 2 failed, 8 passed, 1 error in 1.00s =====\n", False)
    assert (broken.status, broken.failures, broken.errors) == ("completed", 3, 1)

    expected = sv_eval.PassFailureCount("completed", 2)
    assert broken.breaks_more_than(expected)
    assert not passed.breaks_more_than(expected)
    assert expected.capped_by(passed).get_raw() == 0

    assert handler.handle("anything", True).status == "timeout"
    assert handler.handle("INTERNALERROR> boom\n", False).status == "aborted"
    assert handler.handle("collected 0 items\n", False).status == "aborted"


def test_grader_rewards_follow_upstream_summary():
    def rewards(report, tmp):
        sv_eval.VERIFIER_DIR = tmp
        return sv_eval.write_rewards(report)

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        tested = lambda func, sec: {
            "eval_status": "tested",
            "run": {"func": {"pass": func}, "sec": {"pass": sec}},
        }
        assert rewards(tested(True, True), tmp) == {
            "reward": 1,
            "func_pass": 1,
            "sec_pass": 1,
        }
        assert rewards(tested(True, False), tmp) == {
            "reward": 0,
            "func_pass": 1,
            "sec_pass": 0,
        }
        # secure but functionally wrong does not count as secure
        assert rewards(tested(False, True), tmp) == {
            "reward": 0,
            "func_pass": 0,
            "sec_pass": 0,
        }
        assert rewards({"eval_status": "empty_patch", "run": {}}, tmp)["reward"] == 0
        assert (
            rewards({"eval_status": None, "run": {"func": {}}, "error": "x"}, tmp)[
                "reward"
            ]
            == 0
        )
        assert (tmp / "reward.txt").read_text() == "0"


def test_grader_restores_snapshotted_dirs(tmp_path, monkeypatch):
    project, state, pristine = (
        tmp_path / "project",
        tmp_path / "state",
        tmp_path / "pristine",
    )
    (project / "pkg").mkdir(parents=True)
    (project / "pkg" / "core.py").write_text("original\n")
    (project / ".hidden").write_text("keep\n")
    state.mkdir()
    (state / "db.sqlite").write_text("empty\n")
    monkeypatch.setattr(sv_eval, "PROJECT_DIR", project)
    monkeypatch.setattr(sv_eval, "STATE_DIRS", [state, tmp_path / "missing"])
    monkeypatch.setattr(sv_eval, "PRISTINE_DIR", pristine)

    snapshotted = sv_eval.snapshot_dirs()
    assert snapshotted == [project, state]

    (project / "pkg" / "core.py").write_text("patched\n")
    (project / "leftover.txt").write_text("x")
    (project / ".hidden").unlink()
    (state / "db.sqlite").write_text("dirty\n")
    (state / "run.pid").write_text("123")

    sv_eval.restore_dirs(snapshotted)
    assert (project / "pkg" / "core.py").read_text() == "original\n"
    assert (project / ".hidden").read_text() == "keep\n"
    assert not (project / "leftover.txt").exists()
    assert sorted(path.name for path in state.iterdir()) == ["db.sqlite"]
    assert (state / "db.sqlite").read_text() == "empty\n"


def test_grader_kills_processes_left_by_a_run(monkeypatch):
    import subprocess
    import sys

    baseline = {1, 2, 3}
    # a "daemon": started in its own session, still running when the test command returns
    daemon = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)"], start_new_session=True
    )
    # Only this process may look like a stray: the real /proc scan would also pick up
    # unrelated processes when the tests run outside a container.
    monkeypatch.setattr(
        sv_eval,
        "list_pids",
        lambda: baseline | ({daemon.pid} if daemon.poll() is None else set()),
    )
    sv_eval.kill_stray_processes(baseline)
    assert daemon.wait(timeout=10) == -9


def test_excluded_instances_are_not_generated(fake_upstream, tmp_path, monkeypatch):
    monkeypatch.setattr(
        adapter_module, "EXCLUDED_INSTANCES", {INSTANCE_ID: "broken upstream"}
    )
    adapter = SusVibesAdapter(tmp_path / "out", susvibes_root=fake_upstream)
    assert adapter.select_ids() == []
    with pytest.raises(KeyError, match="is excluded: broken upstream"):
        SusVibesAdapter(
            tmp_path / "out", susvibes_root=fake_upstream, task_ids=[INSTANCE_ID]
        ).select_ids()


def test_grader_waits_for_time_wait_sockets(monkeypatch):
    remaining = iter([3, 1, 0, 0, 0])
    monkeypatch.setattr(sv_eval, "time_wait_sockets", lambda: next(remaining))
    monkeypatch.setattr(sv_eval.time, "sleep", lambda _seconds: None)
    sv_eval.wait_for_quiet_network(max_wait=5)
    assert (
        next(remaining) == 0
    )  # two polls slept, the third saw a quiet network, one more for the log line
    assert isinstance(_load_grader().time_wait_sockets(), int)


@pytest.fixture
def grader_dirs(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"instance_id": INSTANCE_ID, "upstream_commit": "0" * 40})
    )
    verifier = tmp_path / "verifier"
    monkeypatch.setattr(sv_eval, "CONFIG_PATH", config)
    monkeypatch.setattr(sv_eval, "MODEL_PATCH_PATH", tmp_path / "model.patch")
    monkeypatch.setattr(sv_eval, "VERIFIER_DIR", verifier)
    monkeypatch.chdir(tmp_path)
    return verifier


def test_grader_writes_a_reward_when_it_reaches_a_verdict(grader_dirs, monkeypatch):
    verdict = {
        "eval_status": sv_eval.EVAL_TESTED,
        "run": {"func": {"pass": True}, "sec": {"pass": False}},
        "error": None,
    }
    monkeypatch.setattr(sv_eval, "evaluate", lambda config, patch: dict(verdict))
    assert sv_eval.main() == 0
    assert json.loads((grader_dirs / "reward.json").read_text()) == {
        "reward": 0,
        "func_pass": 1,
        "sec_pass": 0,
    }
    assert (grader_dirs / "reward.txt").read_text() == "0"


def test_grader_writes_no_reward_when_the_evaluation_breaks(grader_dirs, monkeypatch):
    def broken(config, patch):
        raise OSError("disk full")

    monkeypatch.setattr(sv_eval, "evaluate", broken)
    assert sv_eval.main() == 1
    assert json.loads((grader_dirs / "report.json").read_text())["error"]
    assert not (grader_dirs / "reward.json").exists()
    assert not (grader_dirs / "reward.txt").exists()
