"""The tasks must grade with exactly the pinned latch-eval-tools release."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from latchbio.graders_release import (
    DATA_INSTRUCTIONS,
    LOCK_PATH,
    data_instructions,
    load_lock,
    mismatches,
    package_dir,
)
from latchbio.oracle import load_grade_module
from latchbio.sources import LATCH_EVAL_TOOLS_COMMIT, LATCH_EVAL_TOOLS_REPO, LATCH_EVAL_TOOLS_VERSION


def test_lock_pins_the_release_declared_in_sources():
    lock = load_lock()
    assert lock["package"] == "latch-eval-tools"
    assert lock["version"] == LATCH_EVAL_TOOLS_VERSION
    assert lock["upstream_repo"] == LATCH_EVAL_TOOLS_REPO
    assert lock["upstream_commit"] == LATCH_EVAL_TOOLS_COMMIT


def test_wheel_hash_in_the_dockerfile_is_the_one_uv_locked():
    from latchbio.sources import LATCH_EVAL_TOOLS_WHEEL_SHA256

    lock_text = (Path(__file__).resolve().parents[1] / "uv.lock").read_text()
    assert f"latch_eval_tools-{LATCH_EVAL_TOOLS_VERSION}-py3-none-any.whl" in lock_text
    assert f'hash = "sha256:{LATCH_EVAL_TOOLS_WHEEL_SHA256}"' in lock_text


def test_installed_release_matches_the_lock():
    lock = load_lock()
    assert "graders/numeric.py" in lock["files"] and "types.py" in lock["files"]
    assert mismatches({**lock["files"], **lock["prompt_files"]}) == []
    # Every grader module of the release is locked, so none can change unnoticed.
    modules = {path.relative_to(package_dir()).as_posix() for path in (package_dir() / "graders").glob("*.py")}
    assert modules <= set(lock["files"])


def test_adapter_repository_holds_no_copy_of_the_graders():
    adapter_root = Path(__file__).resolve().parents[1]
    copies = [
        path.relative_to(adapter_root).as_posix()
        for path in adapter_root.rglob("latch_eval_tools")
        if ".venv" not in path.parts
    ]
    assert copies == []


def test_data_instructions_are_the_upstream_prompt_suffix():
    text = data_instructions()
    assert text == (package_dir() / DATA_INSTRUCTIONS).read_text(encoding="utf-8")
    assert "/workspace/data" in text
    assert "/workspace/eval_answer.json" in text


def test_verifier_refuses_graders_that_differ_from_the_lock(tmp_path):
    grade = load_grade_module()
    assert grade.verify_graders()["version"] == LATCH_EVAL_TOOLS_VERSION

    lock = load_lock()
    lock["files"]["graders/numeric.py"] = "0" * 64
    tampered = tmp_path / "graders.lock.json"
    tampered.write_text(json.dumps(lock))
    with pytest.raises(RuntimeError, match="graders/numeric.py"):
        grade.verify_graders(tampered)


def test_verifier_fails_closed_when_the_lock_does_not_match(tmp_path):
    """Run the shipped test.sh logic end to end with a wrong lock: reward 0, non-zero exit."""
    import subprocess
    import sys

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    shutil.copy2(LOCK_PATH.parent / "grade.py", tests_dir / "grade.py")
    lock = load_lock()
    lock["files"]["graders/base.py"] = "f" * 64
    (tests_dir / "graders.lock.json").write_text(json.dumps(lock))
    config = tmp_path / "eval_config.json"
    config.write_text(
        json.dumps({"id": "x", "grader": {"type": "multiple_choice", "config": {"correct_answer": "A"}}})
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "eval_answer.json").write_text(json.dumps({"answer": "A"}))
    output = tmp_path / "logs"
    proc = subprocess.run(
        [
            sys.executable, str(tests_dir / "grade.py"),
            "--config", str(config), "--workspace", str(workspace), "--output-dir", str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "not the pinned release" in proc.stderr
    assert not (output / "reward.txt").exists()  # test.sh then records reward 0


def test_adapter_files_do_not_contain_benchmark_canaries():
    # scripts/validate_adapter.py rejects adapters that embed canary strings;
    # real eval files must be fetched from upstream, never committed here.
    marker = "canary" + " GUID"
    adapter_root = Path(__file__).resolve().parents[1]
    offenders = [
        path.relative_to(adapter_root).as_posix()
        for path in adapter_root.rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".md", ".json", ".toml", ".yaml", ".yml", ".sh", ".txt"}
        and ".venv" not in path.parts
        and marker in path.read_text(errors="ignore")
    ]
    assert offenders == []
