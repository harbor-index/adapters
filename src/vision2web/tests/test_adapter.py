"""Structural and scoring controls; real-input generation is an explicit integration test."""

import asyncio
import importlib.util
import io
import json
import os
import tarfile
import tomllib
from pathlib import Path

import pytest

from vision2web.adapter import Vision2WebAdapter, load_prompts, slug
from vision2web.metrics import aggregate, task_reward
from vision2web.sources import PACKAGE_DIR, catalog, check_upstream, extract_archive


def adapter(tmp_path, **kwargs):
    return Vision2WebAdapter(
        output_dir=tmp_path / "out",
        dataset_dir=tmp_path / "data",
        upstream_dir=tmp_path / "upstream",
        **kwargs,
    )


def test_full_catalog_and_names(tmp_path):
    rows = adapter(tmp_path).selected()
    assert len(rows) == 193
    assert {
        level: sum(r["level"] == level for r in rows)
        for level in ["webpage", "frontend", "website"]
    } == {"webpage": 100, "frontend": 66, "website": 27}
    assert sum(len(r["prototypes"]) for r in rows) == 918
    assert sum(r["num_test_cases"] for r in rows) == 1255
    assert len({slug(row) for row in rows}) == 193


def test_unknown_id_does_not_silently_generate(tmp_path):
    with pytest.raises(ValueError, match="Unknown task"):
        adapter(tmp_path, task_ids=["website/does-not-exist"]).selected()
    assert not (tmp_path / "out").exists()


def test_selection_aliases(tmp_path):
    row = next(r for r in catalog() if r["id"] == "website/filmmovement")
    assert adapter(tmp_path, task_ids=["zai-org/" + slug(row)]).selected() == [row]
    assert len(adapter(tmp_path, split="pilot").selected()) == 3
    with pytest.raises(ValueError):
        adapter(tmp_path, limit=0).selected()


def test_archive_traversal_is_rejected(tmp_path):
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        item = tarfile.TarInfo("webpage/../../escape")
        item.size = 1
        stream.addfile(item, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="Unsafe"):
        extract_archive(archive, tmp_path / "out", "webpage")
    assert not (tmp_path / "escape").exists()


def record(id, level, prototypes, functions=()):
    return {
        "id": id,
        "level": level,
        "prototypes": {str(i): v for i, v in enumerate(prototypes)},
        "function": {str(i): v for i, v in enumerate(functions)},
    }


def test_official_pooling_is_not_mean_of_task_rewards():
    rows = [
        record("a", "webpage", [0]),
        record("b", "webpage", [1] * 9),
        record("c", "frontend", [1], [0]),
        record("d", "frontend", [1], [1] * 9),
        record("e", "website", [1], [1]),
    ]
    value = aggregate(rows)
    assert value["levels"]["webpage"]["score"] == pytest.approx(0.9)
    assert value["levels"]["frontend"]["score"] == pytest.approx(0.95)
    assert value["overall"] == pytest.approx(0.95)
    assert sum(task_reward(r) for r in rows) / len(rows) != pytest.approx(0.95)


def test_missing_levels_are_not_a_full_benchmark_score():
    assert aggregate([record("a", "webpage", [0.5])])["overall"] is None


def test_invalid_or_duplicate_scores_are_rejected():
    with pytest.raises(ValueError):
        task_reward(record("a", "webpage", [float("nan")]))
    with pytest.raises(ValueError):
        aggregate([record("a", "webpage", [1]), record("a", "webpage", [1])])


@pytest.fixture
def verifier(monkeypatch):
    # Load the generated verifier's key gate without importing model clients.
    import sys

    from vision2web import metrics

    monkeypatch.setitem(sys.modules, "metrics", metrics)
    spec = importlib.util.spec_from_file_location(
        "v2w_verify", PACKAGE_DIR / "task-template/tests/verify.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in list(os.environ):
        if name.startswith("VISION2WEB_"):
            monkeypatch.delenv(name)
    return module


def test_model_calls_are_opt_in(verifier):
    with pytest.raises(RuntimeError, match="disabled"):
        verifier.settings({"level": "website"})


def test_l1_needs_only_visual_credentials(verifier, monkeypatch):
    for name, value in {
        "ENABLE_MODEL_GRADING": "1",
        "VISUAL_API_KEY": "test-only",
        "VISUAL_BASE_URL": "https://example.invalid/v1",
    }.items():
        monkeypatch.setenv("VISION2WEB_" + name, value)
    assert verifier.settings({"level": "webpage"})["FUNCTIONAL_API_KEY"] == ""
    with pytest.raises(RuntimeError, match="functional judge"):
        verifier.settings({"level": "frontend"})


def test_disabled_grading_removes_stale_reward(verifier, monkeypatch, tmp_path):
    root, logs = tmp_path / "tests", tmp_path / "logs"
    data = root / "data/webpage/example"
    data.mkdir(parents=True)
    logs.mkdir()
    (root / "task.json").write_text(json.dumps({"level": "webpage", "task_name": "example"}))
    (data / "workflow.json").write_text("[]")
    (logs / "reward.txt").write_text("1")
    monkeypatch.setattr(verifier, "ROOT", root)
    monkeypatch.setattr(verifier, "LOGS", logs)
    with pytest.raises(RuntimeError, match="disabled"):
        asyncio.run(verifier.main())
    assert not (logs / "reward.txt").exists()


@pytest.fixture
def real_sources():
    upstream, dataset = (
        os.environ.get("VISION2WEB_TEST_UPSTREAM"),
        os.environ.get("VISION2WEB_TEST_DATA"),
    )
    if not upstream or not dataset:
        pytest.skip(
            "Set VISION2WEB_TEST_UPSTREAM and VISION2WEB_TEST_DATA for real-input integration"
        )
    return Path(upstream), Path(dataset)


def test_real_task_generation_hides_workflows_and_preserves_prompts(tmp_path, real_sources):
    upstream, dataset = real_sources
    obj = Vision2WebAdapter(tmp_path / "out", dataset, upstream, split="pilot", memory_mb=2048)
    paths = obj.run()
    assert len(paths) == 3
    prompts = load_prompts(upstream)
    for path in paths:
        cfg = tomllib.loads((path / "task.toml").read_text())
        level = cfg["metadata"]["level"]
        assert (path / "instruction.md").read_text() == prompts[level]
        assert not (path / "environment/input/workflow.json").exists()
        assert list((path / "tests/data").glob("*/*/workflow.json"))
        assert cfg["environment"]["memory_mb"] == 2048
        assert cfg["verifier"]["environment_mode"] == "separate"
        assert cfg["task"]["authors"]
        assert (
            json.loads((path / "provenance.json").read_text())["reference_status"] == "not supplied"
        )
    before = {
        str(p.relative_to(tmp_path / "out")): p.read_bytes()
        for p in (tmp_path / "out").rglob("*")
        if p.is_file()
    }
    with pytest.raises(FileExistsError):
        obj.run()
    obj.overwrite = True
    obj.run()
    after = {
        str(p.relative_to(tmp_path / "out")): p.read_bytes()
        for p in (tmp_path / "out").rglob("*")
        if p.is_file()
    }
    assert before == after


def test_changed_upstream_source_is_detected(tmp_path):
    with pytest.raises(ValueError, match="differs"):
        check_upstream(tmp_path)
