"""Unit tests for the TimeMachine-bench adapter.

The generation tests download the pinned upstream JSONL (sha256-checked) into
the user cache on first use.
"""

from __future__ import annotations

import importlib.util
import json
import re
import tarfile
import tomllib
from pathlib import Path

import pytest

from timemachine_bench.adapter import (
    PARITY_TASK_IDS,
    TEMPLATE_DIR,
    TimeMachineBenchAdapter,
    load_records,
    sanitize_task_id,
    upstream_extra_steps,
)
from timemachine_bench.parity_agent.inputs import parse_instruction

CACHE = Path.home() / ".cache" / "harbor-timemachine-bench"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def records():
    return load_records(None, CACHE)


@pytest.fixture(scope="session")
def generated(tmp_path_factory, records):
    out = tmp_path_factory.mktemp("tasks")
    TimeMachineBenchAdapter(out).run()
    return out


def test_sanitize_task_id():
    assert sanitize_task_id("brmc/shortbus") == "brmc__shortbus"
    assert sanitize_task_id("LedgerHQ/marshmallow_dataclass") == "ledgerhq__marshmallow_dataclass"
    assert sanitize_task_id("PennState/canvasapi") == "pennstate__canvasapi"


def test_all_records_generate(records, generated):
    assert len(records) == 100
    dirs = sorted(p.name for p in generated.iterdir())
    assert dirs == sorted(r.task_id for r in records)
    names = set()
    for task_dir in generated.iterdir():
        for rel in (
            "task.toml",
            "instruction.md",
            "environment/Dockerfile",
            "environment/timemachine/patches/patch.diff",
            "environment/timemachine/collect.py",
            "environment/timemachine/env_check.py",
            "environment/timemachine/dependency_versions.txt",
            "environment/timemachine/run_setup.sh",
            "solution/solve.sh",
            "solution/gold.patch",
            "tests/test.sh",
            "tests/evaluate.py",
            "tests/gold.patch",
            "tests/task_info.json",
        ):
            assert (task_dir / rel).is_file(), f"{task_dir.name}: missing {rel}"
        config = tomllib.loads((task_dir / "task.toml").read_text())
        assert config["version"] == "1.0"
        assert re.fullmatch(
            r"tohoku-nlp/timemachine-bench__[a-z0-9][a-z0-9._-]*", config["task"]["name"]
        )
        names.add(config["task"]["name"])
        assert config["verifier"]["environment_mode"] == "separate"
        dockerfile = (task_dir / "environment" / "Dockerfile").read_text()
        assert not re.search(r"__[A-Z_]+__", dockerfile), "unrendered placeholder"
        assert "FROM --platform=linux/amd64 mirror.gcr.io/python:3.12.11@sha256:" in dockerfile
        # /work is snapshotted right after the patches (before any upstream
        # build step) and restored after setup, as upstream's mounted copy is
        steps = [line for line in dockerfile.splitlines() if line.startswith("RUN ")]
        snapshot = steps.index("RUN cp -a /work /opt/timemachine/work-pristine")
        assert steps[snapshot - 1].startswith("RUN patch -p0 -t -E -d /work")
        setup = next(i for i, s in enumerate(steps) if "run_setup.sh" in s)
        assert snapshot < setup
        restore = dockerfile.index("mv /opt/timemachine/work-pristine /work")
        assert dockerfile.index("env_check.py") < restore < dockerfile.index("--write-baseline")
        # nothing the agent could read reveals the reference solution
        assert not (task_dir / "environment" / "gold.patch").exists()
        assert "gold" not in dockerfile.lower()
    assert len(names) == 100


def test_task_ids_are_stable(records):
    # registry names must not change between adapter runs
    assert [r.task_id for r in records][:3] == [
        "brmc__shortbus",
        "jakemcaferty__insurapy",
        "ebispot__gwas-sumstats-validator",
    ]


def test_parity_split(records, tmp_path):
    assert len(PARITY_TASK_IDS) == 10
    assert set(PARITY_TASK_IDS) <= {r.task_id for r in records}
    TimeMachineBenchAdapter(tmp_path, split="parity").run()
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(PARITY_TASK_IDS)


def test_task_ids_accept_upstream_names(tmp_path):
    TimeMachineBenchAdapter(tmp_path, task_ids=["brmc/shortbus", "joway__lemon"]).run()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["brmc__shortbus", "joway__lemon"]


def test_upstream_extra_steps(records):
    extra = {r.repo_name: upstream_extra_steps(r) for r in records}
    assert {k for k, v in extra.items() if v} == {"miracle2k/k8s-snapshots"}


def test_instruction_round_trips_to_upstream_agent_inputs(records, generated):
    for record in records:
        instruction = (generated / record.task_id / "instruction.md").read_text()
        parsed = parse_instruction(instruction)
        assert parsed["test_script"] == record.test_script
        assert parsed["test_files"] == record.test_files
        assert parsed["dependency_versions"] == record.raw["dependency_versions"]
        assert "gold" not in instruction.lower()


def _evaluate_module(tmp_path, monkeypatch):
    evaluate = _load_module(TEMPLATE_DIR / "tests" / "evaluate.py", "tmb_evaluate")
    monkeypatch.setattr(evaluate, "WORK", tmp_path / "work")
    monkeypatch.setattr(evaluate, "SUBMISSION", tmp_path / "artifacts")
    return evaluate


def _collect_module(tmp_path, monkeypatch):
    collect = _load_module(
        TEMPLATE_DIR / "environment" / "timemachine" / "collect.py", "tmb_collect"
    )
    monkeypatch.setattr(collect, "WORK", tmp_path / "work")
    monkeypatch.setattr(collect, "BASELINE", tmp_path / "baseline.json")
    monkeypatch.setattr(collect, "OUT", tmp_path / "artifacts")
    return collect


def test_collect_and_apply(tmp_path, monkeypatch):
    work = tmp_path / "work"
    (work / "pkg").mkdir(parents=True)
    (work / "tests").mkdir()
    (work / "pkg" / "mod.py").write_text("a = 1\nb = 2\n")
    (work / "pkg" / "untouched.py").write_text("x = 1\n")
    (work / "tests" / "test_mod.py").write_text("def test(): pass\n")
    (work / "test_owner__repo.sh").write_text("python -m pytest\n")
    (work / "gone.py").write_text("y = 1\n")

    collect = _collect_module(tmp_path, monkeypatch)
    assert collect.write_baseline() == 0

    # agent: one legitimate edit, edits to protected files, a new file, a deletion
    (work / "pkg" / "mod.py").write_text("a = 1\nb = 3\n")
    (work / "tests" / "test_mod.py").write_text("def test(): assert True\n")
    (work / "test_owner__repo.sh").write_text("exit 0\n")
    (work / "conftest.py").write_text("raise SystemExit\n")
    (work / "gone.py").unlink()
    assert collect.collect() == 0
    summary = json.loads((tmp_path / "artifacts" / "summary.json").read_text())
    assert summary["changed"] == ["pkg/mod.py", "test_owner__repo.sh", "tests/test_mod.py"]
    assert summary["deleted"] == ["gone.py"]
    assert summary["new"] == ["conftest.py"]

    # verifier: a fresh copy of the starting /work
    (work / "pkg" / "mod.py").write_text("a = 1\nb = 2\n")
    (work / "tests" / "test_mod.py").write_text("def test(): pass\n")
    (work / "test_owner__repo.sh").write_text("python -m pytest\n")
    (work / "conftest.py").unlink()
    (work / "gone.py").write_text("y = 1\n")

    evaluate = _evaluate_module(tmp_path, monkeypatch)
    protected = {"tests/test_mod.py", "setup_owner__repo.sh", "test_owner__repo.sh"}
    applied, discarded, originals = evaluate.apply_submission(protected)
    assert applied == ["pkg/mod.py"]
    assert {d["path"] for d in discarded} == {"tests/test_mod.py", "test_owner__repo.sh"}
    assert (work / "pkg" / "mod.py").read_text() == "a = 1\nb = 3\n"
    assert (work / "tests" / "test_mod.py").read_text() == "def test(): pass\n"
    assert (work / "test_owner__repo.sh").read_text() == "python -m pytest\n"
    assert not (work / "conftest.py").exists()
    assert (work / "gone.py").exists()
    assert originals == {"pkg/mod.py": b"a = 1\nb = 2\n"}


def test_apply_rejects_unsafe_members(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    (work / "ok.py").write_text("1\n")
    outside = tmp_path / "outside.py"
    outside.write_text("secret\n")
    (work / "link.py").symlink_to(outside)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    src = tmp_path / "payload.py"
    src.write_text("pwned\n")
    with tarfile.open(artifacts / "changes.tar", "w") as tar:
        tar.add(src, arcname="../outside.py")
        tar.add(src, arcname="/etc/passwd")
        tar.add(src, arcname="link.py")
        tar.add(src, arcname="ok.py")
    evaluate = _evaluate_module(tmp_path, monkeypatch)
    applied, discarded, _ = evaluate.apply_submission(set())
    assert applied == ["ok.py"]
    assert len(discarded) == 3
    assert outside.read_text() == "secret\n"


def test_prec_matches_upstream_definition(tmp_path):
    evaluate = _load_module(TEMPLATE_DIR / "tests" / "evaluate.py", "tmb_evaluate_prec")
    gold = tmp_path / "gold.patch"
    gold.write_text("--- pkg/a.py\n+++ pkg/a.py\n@@ -1,3 +1,3 @@\n x = 1\n-y = 2\n+y = 3\n z = 4\n")
    model = tmp_path / "model.patch"
    model.write_text(
        "--- pkg/a.py\n+++ pkg/a.py\n@@ -1,4 +1,4 @@\n x = 1\n-y = 2\n+y = 3\n z = 4\n-w = 5\n+w = 6\n"
    )
    gold_edits = evaluate.extract_edits_from_diff(gold)
    assert evaluate.calc_prec(gold_edits, gold_edits) == 1.0
    assert evaluate.calc_prec(gold_edits, evaluate.extract_edits_from_diff(model)) == 0.5
    empty = tmp_path / "empty.patch"
    empty.write_text("")
    assert evaluate.calc_prec(gold_edits, evaluate.extract_edits_from_diff(empty)) == 0


def test_generated_tasks_contain_no_bytecode(generated):
    assert not list(generated.rglob("__pycache__"))
    assert not list(generated.rglob("*.pyc"))


def test_env_check_normalization():
    env_check = _load_module(
        TEMPLATE_DIR / "environment" / "timemachine" / "env_check.py", "tmb_env_check"
    )
    recorded = [
        "PyYAML==6.0.2",
        "# Editable install with no version control (k8s-snapshots==0.1.0)",
        "-e /work",
        "choix @ file:///work",
        "typing_extensions==4.14.1",
    ]
    frozen = ["pyyaml==6.0.2", "typing-extensions==4.14.1", "-e /work"]
    assert env_check.requirements(recorded) == env_check.requirements(frozen)
    assert env_check.requirements(["numpy==2.3.2"]) != env_check.requirements(["numpy==1.26.4"])
