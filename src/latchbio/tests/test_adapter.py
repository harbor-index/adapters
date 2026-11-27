"""Task generation against a synthetic upstream repository."""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from latchbio.adapter import LatchBioAdapter
from latchbio.data import DataError, DataFile, DataManifest, DataNode
from latchbio.sources import AGENT_IMAGE, BENCHMARKS
from latchbio.upstream import UpstreamError, ensure_checkout, load_evals, sanitize_id

from conftest import EVALS

TASK_IDS = [
    "fakebench__marker_panel",
    "fakebench__multi_grader",
    "fakebench__qc01_cell_counts",
]


def make_adapter(tmp_path: Path, upstream_root: Path, **kwargs) -> LatchBioAdapter:
    kwargs.setdefault("data_source", "none")
    return LatchBioAdapter(
        tmp_path / "out", upstream_root=upstream_root, cache_dir=tmp_path / "cache", **kwargs
    )


def test_sanitize_id_lowercases_and_replaces_unsafe_characters():
    assert sanitize_id("DE01_pseudobulk_de") == "de01_pseudobulk_de"
    assert sanitize_id(" T04a endothelin/niche ") == "t04a-endothelin-niche"
    with pytest.raises(ValueError):
        sanitize_id("///")


def test_pinned_sources_are_unique_and_fully_pinned():
    assert len({source.key for source in BENCHMARKS}) == len(BENCHMARKS)
    for source in BENCHMARKS:
        assert len(source.commit) == 40 and int(source.commit, 16) >= 0
        assert source.repo.startswith("https://github.com/latchbio/")
        assert source.authors
    assert "@sha256:" in AGENT_IMAGE


def test_only_eval_definitions_become_tasks(patch_sources, upstream_root, tmp_path):
    adapter = make_adapter(tmp_path, upstream_root)
    assert sorted(item.task_id for item in adapter.load_all_evals()) == TASK_IDS


def test_generated_task_layout(patch_sources, upstream_root, tmp_path):
    adapter = make_adapter(tmp_path, upstream_root)
    adapter.run()
    out = tmp_path / "out"
    assert sorted(path.name for path in out.iterdir()) == TASK_IDS
    for task_id in TASK_IDS:
        task = out / task_id
        for rel in (
            "task.toml",
            "instruction.md",
            "environment/Dockerfile",
            "solution/solve.sh",
            "solution/eval_answer.json",
            "tests/test.sh",
            "tests/grade.py",
            "tests/eval_config.json",
            "tests/graders.lock.json",
        ):
            assert (task / rel).is_file(), f"{task_id}/{rel}"
        assert not list(task.rglob("__pycache__"))
        # The graders are installed in the image, never copied into the task.
        assert not list(task.rglob("latch_eval_tools"))
        dockerfile = (task / "environment" / "Dockerfile").read_text()
        assert "latch-eval-tools==0.4.54 --hash=sha256:" in dockerfile and "--require-hashes" in dockerfile
        assert "{" not in dockerfile  # every placeholder was filled
    # Nothing but finished task directories is left in the output directory.
    assert not [path for path in out.iterdir() if path.name.startswith(".")]


def test_task_toml_is_valid_and_credits_upstream(patch_sources, upstream_root, tmp_path):
    make_adapter(tmp_path, upstream_root).run()
    config = tomllib.loads((tmp_path / "out" / "fakebench__qc01_cell_counts" / "task.toml").read_text())
    assert config["schema_version"] == "1.0"
    assert config["task"]["name"] == "latchbio/fakebench__qc01_cell_counts"
    assert config["task"]["authors"] == [
        {"name": "Ada Lovelace"},
        {"name": "Grace Hopper", "email": "grace@example.com"},
    ]
    assert config["metadata"]["eval_id"] == "QC01_cell_counts"
    assert config["metadata"]["grader_types"] == ["numeric_tolerance"]
    assert config["metadata"]["data_nodes"] == ["latch://1001.node"]
    assert config["metadata"]["upstream_commit"] == patch_sources.commit
    assert config["metadata"]["upstream_path"] == "evals/qc/QC01_cell_counts.json"
    assert config["agent"]["timeout_sec"] == 21600.0
    assert config["environment"]["cpus"] == 4
    assert config["environment"]["memory_mb"] == 32768
    assert config["environment"]["workdir"] == "/workspace"

    nested = tomllib.loads((tmp_path / "out" / "fakebench__marker_panel" / "task.toml").read_text())
    assert nested["metadata"]["grader_types"] == [
        "all_of",
        "marker_gene_precision_recall",
        "label_set_jaccard",
        "multiple_choice",
    ]
    assert "none" not in nested["task"]["keywords"]


def test_instruction_is_upstream_task_plus_data_instructions(patch_sources, upstream_root, tmp_path):
    adapter = make_adapter(tmp_path, upstream_root)
    adapter.run()
    raw = EVALS["evals/qc/QC01_cell_counts.json"]
    text = (tmp_path / "out" / "fakebench__qc01_cell_counts" / "instruction.md").read_text()
    # latch_eval_tools.harness.utils.prompt_with_suffix: f"{task_prompt}\n{prompt_suffix}"
    assert text == f"{raw['task']}\n{adapter.data_instructions}"


def test_instruction_and_environment_do_not_leak_the_answer(patch_sources, upstream_root, tmp_path):
    make_adapter(tmp_path, upstream_root).run()
    task = tmp_path / "out" / "fakebench__qc01_cell_counts"
    agent_visible = (task / "instruction.md").read_text() + "".join(
        path.read_text() for path in (task / "environment").rglob("*") if path.is_file()
    )
    assert "1500" not in agent_visible and "68.5" not in agent_visible
    assert "Ground truth derived" not in agent_visible
    # ...while the verifier and the oracle do carry it.
    assert json.loads((task / "tests" / "eval_config.json").read_text())["grader"] == EVALS[
        "evals/qc/QC01_cell_counts.json"
    ]["grader"]
    assert "Ground truth derived" in (task / "solution" / "upstream_notes.md").read_text()


def test_oracle_answer_passes_the_shipped_verifier(patch_sources, upstream_root, tmp_path):
    make_adapter(tmp_path, upstream_root).run()
    for task_id in TASK_IDS:
        task = tmp_path / "out" / task_id
        workspace = tmp_path / "ws" / task_id
        workspace.mkdir(parents=True)
        (workspace / "eval_answer.json").write_text((task / "solution" / "eval_answer.json").read_text())
        logs = tmp_path / "logs" / task_id
        subprocess.run(
            [
                sys.executable, str(task / "tests" / "grade.py"),
                "--config", str(task / "tests" / "eval_config.json"),
                "--workspace", str(workspace),
                "--output-dir", str(logs),
            ],
            check=True,
            capture_output=True,
        )
        assert (logs / "reward.txt").read_text().strip() == "1", task_id


def test_generation_is_deterministic(patch_sources, upstream_root, tmp_path):
    def snapshot(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    make_adapter(tmp_path / "a", upstream_root).run()
    make_adapter(tmp_path / "b", upstream_root).run()
    assert snapshot(tmp_path / "a" / "out") == snapshot(tmp_path / "b" / "out")


def test_task_ids_accept_harbor_ids_registry_names_and_upstream_ids(patch_sources, upstream_root, tmp_path):
    for selector in ("fakebench__qc01_cell_counts", "latchbio/fakebench__qc01_cell_counts", "QC01_cell_counts"):
        adapter = make_adapter(tmp_path, upstream_root, task_ids=[selector])
        assert [item.task_id for item in adapter.select_evals()] == ["fakebench__qc01_cell_counts"]
    with pytest.raises(ValueError, match="No such task"):
        make_adapter(tmp_path, upstream_root, task_ids=["missing"]).select_evals()


def test_limit_and_overwrite(patch_sources, upstream_root, tmp_path):
    make_adapter(tmp_path, upstream_root, limit=1).run()
    out = tmp_path / "out"
    assert len(list(out.iterdir())) == 1
    only = next(out.iterdir())
    marker = only / "instruction.md"
    marker.write_text("edited")

    make_adapter(tmp_path, upstream_root, limit=1).run()
    assert marker.read_text() == "edited"  # existing tasks are kept without --overwrite

    make_adapter(tmp_path, upstream_root, limit=1, overwrite=True).run()
    assert marker.read_text() != "edited"


def test_unknown_benchmark_and_data_source_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="Unknown benchmark"):
        LatchBioAdapter(tmp_path, benchmarks=["nope"])
    with pytest.raises(ValueError, match="data_source"):
        LatchBioAdapter(tmp_path, data_source="s3")


def test_none_data_source_stages_no_data(patch_sources, upstream_root, tmp_path):
    make_adapter(tmp_path, upstream_root).run()
    env = tmp_path / "out" / "fakebench__qc01_cell_counts" / "environment"
    assert sorted(path.name for path in env.iterdir()) == ["Dockerfile"]
    dockerfile = (env / "Dockerfile").read_text()
    assert f"FROM --platform=linux/amd64 {AGENT_IMAGE}" in dockerfile
    assert "{" not in dockerfile.replace("${", "")


def test_mirror_data_source_requires_a_published_mirror(patch_sources, upstream_root, tmp_path):
    adapter = make_adapter(tmp_path, upstream_root, data_source="mirror")
    adapter.manifest = DataManifest()
    with pytest.raises(DataError, match="does not name a data mirror"):
        adapter.run()


def test_mirror_data_source_writes_pinned_download_manifest(patch_sources, upstream_root, tmp_path):
    adapter = make_adapter(tmp_path, upstream_root, data_source="mirror", task_ids=["marker_panel"])
    nodes = {
        "latch://1002.node": DataNode(
            "latch://1002.node", "counts.h5ad", "file", (DataFile("counts.h5ad", 10, "a" * 64),)
        ),
        "latch://1003.node": DataNode(
            "latch://1003.node",
            "refs",
            "dir",
            (DataFile("refs/genes.gtf", 20, "b" * 64), DataFile("refs/sub/x.fa", 30, "c" * 64)),
        ),
    }
    adapter.manifest = DataManifest(nodes=nodes, mirror_base_url="https://mirror.example/latchbio")
    adapter.run()

    env = tmp_path / "out" / "fakebench__marker_panel" / "environment"
    assert sorted(path.name for path in env.iterdir()) == ["Dockerfile", "data_manifest.json", "fetch_data.py"]
    manifest = json.loads((env / "data_manifest.json").read_text())
    assert [entry["path"] for entry in manifest["files"]] == [
        "counts.h5ad",
        "refs/genes.gtf",
        "refs/sub/x.fa",
    ]
    assert manifest["files"][1]["url"] == "https://mirror.example/latchbio/1003/refs/genes.gtf"
    assert "fetch_data.py" in (env / "Dockerfile").read_text()


def test_mirror_data_source_rejects_unpinned_nodes(patch_sources, upstream_root, tmp_path):
    adapter = make_adapter(tmp_path, upstream_root, data_source="mirror", task_ids=["marker_panel"])
    adapter.manifest = DataManifest(mirror_base_url="https://mirror.example/latchbio")
    with pytest.raises(DataError, match="not in data_manifest.json"):
        adapter.run()


def test_upstream_root_must_be_at_the_pinned_commit(fake_source, upstream_root, tmp_path):
    repo = upstream_root / "fakebench"
    (repo / "evals" / "extra.txt").write_text("drift")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "drift"],
        cwd=repo,
        check=True,
    )
    with pytest.raises(UpstreamError, match="pins FakeBench"):
        ensure_checkout(fake_source, cache_dir=tmp_path / "cache", upstream_root=upstream_root)


def test_evals_without_grader_or_data_are_reported(fake_source, upstream_root):
    repo = upstream_root / "fakebench"
    (repo / "evals" / "withheld.json").write_text(json.dumps({"id": "withheld", "task": "t"}))
    with pytest.raises(UpstreamError, match="exactly one of 'grader' or 'graders'"):
        load_evals(fake_source, repo)


def test_small_data_split_keeps_evals_with_small_input_data(patch_sources, upstream_root, tmp_path):
    from latchbio.data import SMALL_DATA_MAX_BYTES

    def node(uri: str, size: int) -> DataNode:
        name = f"{uri.rsplit('/', 1)[-1]}.bin"
        return DataNode(uri, name, "file", (DataFile(name, size, "a" * 64),))

    adapter = make_adapter(tmp_path, upstream_root, split="small-data")
    adapter.manifest = DataManifest(
        nodes={
            "latch://1001.node": node("latch://1001.node", SMALL_DATA_MAX_BYTES),  # at the limit
            "latch://1002.node": node("latch://1002.node", SMALL_DATA_MAX_BYTES),
            "latch://1003.node": node("latch://1003.node", 1),  # pushes marker_panel over
            "latch://1004.node": node("latch://1004.node", 10),
        }
    )
    assert sorted(item.task_id for item in adapter.select_evals()) == [
        "fakebench__multi_grader",
        "fakebench__qc01_cell_counts",
    ]
    adapter.split = "full"
    assert len(adapter.select_evals()) == 3


def test_small_data_split_needs_pinned_data_sizes(patch_sources, upstream_root, tmp_path):
    adapter = make_adapter(tmp_path, upstream_root, split="small-data")
    adapter.manifest = DataManifest()
    with pytest.raises(DataError, match="not in data_manifest.json"):
        adapter.select_evals()


def test_unknown_split_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="split"):
        LatchBioAdapter(tmp_path, split="lite")


def test_list_valued_upstream_metadata_is_rendered_as_lists(patch_sources, upstream_root, tmp_path):
    # VariantBench's sv_chromosome_end_signal has kit = ["pacbio", "ont"].
    repo = upstream_root / "fakebench"
    path = repo / "evals" / "qc" / "QC01_cell_counts.json"
    raw = json.loads(path.read_text())
    raw["metadata"]["kit"] = ["pacbio", "ont"]
    path.write_text(json.dumps(raw))
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "list kit"],
        cwd=repo,
        check=True,
    )
    import dataclasses

    import latchbio.adapter as adapter_module

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    source = dataclasses.replace(patch_sources, commit=head)
    adapter_module.BENCHMARKS = (source,)

    make_adapter(tmp_path, upstream_root, task_ids=["QC01_cell_counts"]).run()
    config = tomllib.loads((tmp_path / "out" / "fakebench__qc01_cell_counts" / "task.toml").read_text())
    assert config["metadata"]["kit"] == ["pacbio", "ont"]
    assert config["task"]["description"] == "FakeBench evaluation QC01_cell_counts (qc, pacbio/ont)"
    assert {"pacbio", "ont"} <= set(config["task"]["keywords"])
    assert not any("[" in keyword for keyword in config["task"]["keywords"])


def test_placeholder_like_text_in_upstream_content_is_left_alone():
    from latchbio.adapter import _fill

    task = "Report {data_instructions} and {task} literally; also {metadata}."
    rendered = _fill("{task}\n{data_instructions}", {"task": task, "data_instructions": "SUFFIX"})
    assert rendered == f"{task}\nSUFFIX"


def test_interrupted_upstream_fetch_does_not_poison_the_cache(fake_source, upstream_root, tmp_path):
    import dataclasses

    source = dataclasses.replace(fake_source, repo=str(upstream_root / "fakebench"))
    cache = tmp_path / "cache"
    broken = cache / f"{source.key}-{source.commit[:12]}"
    broken.mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet"], cwd=broken, check=True)  # .git but no commit

    checkout = ensure_checkout(source, cache_dir=cache)
    assert checkout == broken
    assert len(load_evals(source, checkout)) == 3
    assert [p.name for p in cache.iterdir()] == [broken.name]  # no scratch dirs left behind
