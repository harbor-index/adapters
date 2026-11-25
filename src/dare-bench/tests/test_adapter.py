"""Generation tests. Set DARE_SOURCE_DIR to a dare-bench checkout (data/eval, scripts, LICENSE)."""

import json
import os
from pathlib import Path

import pytest

from dare_bench.adapter import FAMILY, TEMPLATE_DIR, DAREBenchAdapter, task_id_for

SOURCE = Path(os.environ.get("DARE_SOURCE_DIR", ""))
needs_source = pytest.mark.skipif(not (SOURCE / "data/eval/question_list.json").is_file(), reason="DARE_SOURCE_DIR not set")

TABULAR_V1 = "vishushekhar_pokemonunitedataset_class-v1"
TS_V2 = "abdelrahmannasrsleem_us-monthly-retail-and-food-services-sales_ts-v2"


def test_task_id_is_stable_and_safe():
    assert task_id_for("Some_Dataset.Name_class", "v1") == "some_dataset-name_class-v1"


@needs_source
def test_scorer_is_verbatim_upstream_copy():
    assert (TEMPLATE_DIR / "tests/dare_evaluation.py").read_bytes() == (SOURCE / "scripts/evaluation.py").read_bytes()


@needs_source
def test_full_split_has_324_tasks_and_parity_has_five_per_family():
    adapter = DAREBenchAdapter(Path("/nonexistent"), source_dir=SOURCE)
    tasks = adapter.all_tasks()
    assert len(tasks) == 324
    parity = DAREBenchAdapter(Path("/nonexistent"), source_dir=SOURCE, split="parity").selected_tasks()
    assert len(parity) == 30
    by_family = {}
    for t in parity:
        by_family.setdefault(FAMILY[(t["entry"]["task"], t["variant"])], []).append(t)
    assert sorted(by_family) == ["CF", "IF", "MM", "XF"]
    assert len(by_family["IF"]) == 10 and len(by_family["MM"]) == 10
    # deterministic
    again = DAREBenchAdapter(Path("/nonexistent"), source_dir=SOURCE, split="parity").selected_tasks()
    assert [t["task_id"] for t in parity] == [t["task_id"] for t in again]


@needs_source
@pytest.mark.parametrize("task_id", [TABULAR_V1, TS_V2])
def test_generated_task_hides_labels_from_agent(tmp_path, task_id):
    DAREBenchAdapter(tmp_path, source_dir=SOURCE, task_ids=[task_id]).run()
    task = tmp_path / task_id
    entry_id, variant = task_id.rsplit("-", 1)
    question = next(q for q in json.loads((SOURCE / "data/eval/question_list.json").read_text()) if q["file_path"].lower() == entry_id)

    visible = sorted(p.name for p in (task / "environment/workspace").iterdir())
    expected = sorted(set(question[f"needed_files_{variant}"]) | {"metadata.txt"})
    assert visible == expected
    env_files = {p.name for p in (task / "environment").rglob("*")}
    for hidden in ("ground_truth.csv", "ground_truth_v1.csv", "ground_truth_v2.csv", "all_metadata.json", "simulated_pred_local.csv"):
        assert hidden not in env_files
    assert not any(name.startswith("train_v1_no_err") for name in env_files)
    assert question[f"question_{variant}"] in (task / "instruction.md").read_text()


@needs_source
def test_regeneration_is_byte_identical(tmp_path):
    for out in (tmp_path / "a", tmp_path / "b"):
        DAREBenchAdapter(out, source_dir=SOURCE, task_ids=[TABULAR_V1, TS_V2]).run()
    files_a = sorted(p.relative_to(tmp_path / "a") for p in (tmp_path / "a").rglob("*") if p.is_file())
    files_b = sorted(p.relative_to(tmp_path / "b") for p in (tmp_path / "b").rglob("*") if p.is_file())
    assert files_a == files_b
    for rel in files_a:
        assert (tmp_path / "a" / rel).read_bytes() == (tmp_path / "b" / rel).read_bytes(), rel
