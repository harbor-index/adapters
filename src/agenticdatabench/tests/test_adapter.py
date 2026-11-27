"""Generation tests on a tiny fake source repo (no network)."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from agenticdatabench import adapter as ad

CHART = {
    "id": "agriculture_14",
    "question": "Plot it and save output.png; save the max to output.csv.",
    "domain": "agriculture",
    "output_file_name": ["output.png", "output.csv"],
    "gold_file_name": ["result.png", "result.csv"],
    "eval_func": [
        "compare_image('output.png', 'result.png', calculate_columns=['type'])",
        "compare_csv(output_file_name='output.csv', gold_file_name='result.csv', ignore_order=True)",
    ],
}
PLAIN = {
    "id": "loan_risk_5",
    "question": "Summarise input.csv into output.csv.",
    "domain": "loan_risk/loan_risk_5",
    "output_file_name": ["output.csv"],
    "gold_file_name": ["result.csv"],
    "eval_func": "compare_csv(output_file_name='output.csv', gold_file_name='result.csv')",
}


def _filler(i: int) -> dict:
    return {
        "id": f"tourism_{i:03d}",
        "question": "q",
        "domain": "tourism",
        "output_file_name": ["output.csv"],
        "gold_file_name": ["result.csv"],
        "eval_func": "compare_csv('output.csv', 'result.csv')",
    }


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "AgenticDataBench"
    tasks = [CHART, PLAIN] + [_filler(i) for i in range(244)]
    (root / "testbed" / "tasks").mkdir(parents=True)
    (root / "testbed" / "tasks" / "dev.jsonl").write_text("".join(json.dumps(t) + "\n" for t in tasks))
    for t in tasks:
        gold = root / "testbed" / "gold" / t["id"]
        gold.mkdir(parents=True)
        for g in ad.as_list(t["gold_file_name"]):
            (gold / g).write_text(f"gold {g}")
    chart_gold = root / "testbed" / "gold" / "agriculture_14"
    (chart_gold / "result.json").write_text("{}")
    (chart_gold / "result.npy").write_bytes(b"npy")
    return root


@pytest.fixture
def data(monkeypatch):
    files = {
        "agriculture": [ad.DataFile("datasets/agriculture/a b.csv", "a b.csv", 300 * 2**20, "sha256", "ab")],
        "loan_risk/loan_risk_5": [
            ad.DataFile("datasets/loan_risk/loan_risk_5/input.csv", "input.csv", 2 * 2**30, "sha256", "cd")
        ],
        "tourism": [ad.DataFile("datasets/tourism/t.csv", "t.csv", 10, "gitsha1", "ef")],
    }
    monkeypatch.setattr(ad.AgenticDataBenchAdapter, "_data_files", staticmethod(lambda: files))
    return files


def test_generates_every_task_not_excluded(source, data, tmp_path):
    out = tmp_path / "out"
    ad.AgenticDataBenchAdapter(out, source_dir=source).run()
    names = {p.name for p in out.iterdir()}
    all_ids = {json.loads(line)["id"] for line in (source / "testbed" / "tasks" / "dev.jsonl").read_text().splitlines()}
    assert names == all_ids - ad.EXCLUDED_TASKS.keys()


def test_instruction_is_upstream_prompt(source, data, tmp_path):
    out = tmp_path / "out"
    ad.AgenticDataBenchAdapter(out, source_dir=source, task_ids=["agriculture_14", "loan_risk_5"]).run()
    plain = (out / "loan_risk_5" / "instruction.md").read_text()
    assert plain == (
        "Summarise input.csv into output.csv.\n\nYou are working in the directory: /workspace."
        " All required data files are available in this directory."
        " Complete the task and ensure all output files are saved in this directory."
    )
    chart = (out / "agriculture_14" / "instruction.md").read_text()
    assert "saved in this directory.\n### Plotting (REQUIRED)" in chart
    assert "Use ONLY these file names:\noutput.png\n" in chart
    assert 'Plotprocess.plot_process(fig, "output.png")' in chart
    assert chart.endswith("```") and not chart.endswith("\n")


def test_chart_task_files(source, data, tmp_path):
    out = tmp_path / "out"
    ad.AgenticDataBenchAdapter(out, source_dir=source, task_ids=["agriculture_14"]).run()
    t = out / "agriculture_14"
    assert (t / "environment" / "image.py").exists()
    assert "COPY image.py /workspace/image.py" in (t / "environment" / "Dockerfile").read_text()
    sol = t / "solution" / "output"
    assert sorted(p.name for p in sol.iterdir()) == ["output.csv", "output.json", "output.npy", "output.png"]
    assert (sol / "output.csv").read_text() == "gold result.csv"
    assert sorted(p.name for p in (t / "tests" / "gold").iterdir()) == [
        "result.csv", "result.json", "result.npy", "result.png"
    ]
    cfg = json.loads((t / "tests" / "task_config.json").read_text())
    assert cfg["eval_func"] == CHART["eval_func"] and "question" not in cfg
    toml = tomllib.loads((t / "task.toml").read_text())
    assert toml["task"]["name"] == "agenticdatabench/agenticdatabench__agriculture_14"
    assert toml["task"]["authors"][0] == {"name": "Zhaoyan Sun"}
    assert toml["metadata"]["output_types"] == ["csv", "png"]
    assert toml["environment"]["memory_mb"] == 8192
    assert toml["verifier"]["environment_mode"] == "separate"
    assert toml["artifacts"] == [
        "/workspace/output.png", "/workspace/output.csv", "/workspace/output.json", "/workspace/output.npy"
    ]


def test_plain_task_files(source, data, tmp_path):
    out = tmp_path / "out"
    ad.AgenticDataBenchAdapter(out, source_dir=source, task_ids=["loan_risk_5"]).run()
    t = out / "loan_risk_5"
    assert not (t / "environment" / "image.py").exists()
    dockerfile = (t / "environment" / "Dockerfile").read_text()
    assert "image.py" not in dockerfile and "{" not in dockerfile
    manifest = (t / "environment" / "data_manifest.tsv").read_text().splitlines()
    assert manifest == [
        f"{ad.DATA_REVISION}\tdatasets/loan_risk/loan_risk_5/input.csv\tinput.csv\t{2 * 2**30}\tsha256\tcd"
    ]
    toml = tomllib.loads((t / "task.toml").read_text())
    assert toml["metadata"]["domain"] == "loan_risk"
    assert toml["environment"]["memory_mb"] == 16384
    assert toml["environment"]["storage_mb"] >= 2 * 2048 + 8192
    assert toml["verifier"]["environment_mode"] == "separate"
    assert toml["artifacts"] == ["/workspace/output.csv"]


def test_artifacts_are_exactly_the_oracle_files(source, data, tmp_path):
    # The separate verifier sees only the artifacts, so they must cover every
    # file the oracle writes (or the oracle would score below 1.0), and nothing
    # else is needed by the grader.
    out = tmp_path / "out"
    ad.AgenticDataBenchAdapter(out, source_dir=source, task_ids=["agriculture_14", "loan_risk_5"]).run()
    for t in out.iterdir():
        toml = tomllib.loads((t / "task.toml").read_text())
        oracle = sorted(f"/workspace/{p.name}" for p in (t / "solution" / "output").iterdir())
        assert sorted(toml["artifacts"]) == oracle, t.name


def test_rendered_files_have_no_placeholders(source, data, tmp_path):
    out = tmp_path / "out"
    ad.AgenticDataBenchAdapter(out, source_dir=source, task_ids=["agriculture_14", "loan_risk_5"]).run()
    for t in out.iterdir():
        for name in ("task.toml", "environment/Dockerfile"):
            text = (t / name).read_text()
            for key in ("{task_id}", "{domain", "{image_helper}", "{memory_mb}", "{storage_mb}", "{output_types}",
                        "{artifacts}"):
                assert key not in text, (t.name, name, key)


def test_overwrite_and_skip(source, data, tmp_path):
    out = tmp_path / "out"
    ad.AgenticDataBenchAdapter(out, source_dir=source, task_ids=["loan_risk_5"]).run()
    marker = out / "loan_risk_5" / "marker"
    marker.write_text("x")
    ad.AgenticDataBenchAdapter(out, source_dir=source, task_ids=["loan_risk_5"]).run()
    assert marker.exists()
    ad.AgenticDataBenchAdapter(out, source_dir=source, task_ids=["loan_risk_5"], overwrite=True).run()
    assert not marker.exists()


# The 229 kept task ids at the pinned commit, by domain.
KEPT_IDS = {
    "agriculture": "02 03 04 06 07 09 10 12 14 15 16 18 20 21 22 24 25 28 29 30 31 32",
    "ecommerce": "01 03 06 07 10 13 14 15 16 21 26 30 32 35",
    "energy": "01 03 09 10",
    "entertainment": "02 03 04 07 08",
    "financial": "163 167 234 236 270 276 290 293 297 310 311 317 318 325 364 365 425 44 52 536 71 83 84 95",
    "healthcare": "01 02 03 04 05 06 08 11 12 13 14 15 16 17 18 19 20 23 24 25 26 27 31 33",
    "loan_model": "18 20",
    "loan_risk": "1 10 11 13 14 15 2 20 25 3 31 33 35 36 37 4 41 42 43 44 5 6 7 8 9",
    "real_estate": "01 03 04 06 07 08 09 11 12 13 14 15 16 18 19 21 22 25 26 27 29 30 31 32",
    "social_network": "01 02 05 06 08 10 11 16 18 21 22 23 24 25 26 27 30 31 33",
    "sports": "06 07 08 09 11 13 14 16 17 20 24 25 27 28 30 32 33",
    "strategy": "2 3 5 7",
    "tourism": "01 02 04 05 08 10 11 12 14 15 16 17 19 20 23 24 25 28 29 30 31 32",
    "transportation": "02 05 06 07 08 10 11 13 16 18 19 21 22 23 24 26 27 28 29 30 31 32 35",
}
# The subset parity ran on (`--split parity`).
PARITY_TASKS = {
    "agriculture_03", "agriculture_14", "agriculture_20", "agriculture_21", "agriculture_25",
    "ecommerce_10", "ecommerce_14", "ecommerce_15", "energy_10", "entertainment_04",
    "financial_52", "financial_270", "financial_290", "financial_310", "financial_425",
    "healthcare_04", "healthcare_05", "healthcare_12", "healthcare_23", "healthcare_25",
    "loan_model_18", "loan_risk_3", "loan_risk_6", "loan_risk_8", "loan_risk_11", "loan_risk_13",
    "real_estate_06", "real_estate_14", "real_estate_15", "real_estate_21", "real_estate_25",
    "social_network_11", "social_network_22", "social_network_25", "social_network_27",
    "sports_06", "sports_17", "sports_27", "sports_30", "strategy_2",
    "tourism_01", "tourism_04", "tourism_19", "tourism_31", "tourism_32",
    "transportation_13", "transportation_19", "transportation_26", "transportation_30", "transportation_35",
}


def test_parity_split_is_the_fixed_subset():
    ids = [f"{d}_{n}" for d, nums in KEPT_IDS.items() for n in nums.split()]
    assert len(ids) == 229
    assert not set(ids) & ad.EXCLUDED_TASKS.keys()
    assert ad.AgenticDataBenchAdapter.parity_ids(ids) == PARITY_TASKS
    assert ad.AgenticDataBenchAdapter.parity_ids(list(reversed(ids))) == PARITY_TASKS
    assert len(PARITY_TASKS) == ad.PARITY_SIZE
    # every domain is in it
    assert {t.rsplit("_", 1)[0] for t in PARITY_TASKS} == KEPT_IDS.keys()


def test_parity_split_generates_only_the_subset(source, data, tmp_path):
    out = tmp_path / "out"
    ad.AgenticDataBenchAdapter(out, source_dir=source, split="parity").run()
    names = {p.name for p in out.iterdir()}
    all_ids = [json.loads(line)["id"] for line in (source / "testbed" / "tasks" / "dev.jsonl").read_text().splitlines()]
    assert names == ad.AgenticDataBenchAdapter.parity_ids([i for i in all_ids if i not in ad.EXCLUDED_TASKS])
    assert len(names) == ad.PARITY_SIZE
    assert {"agriculture_14", "loan_risk_5"} <= names  # one-task domains still get one


def test_unknown_split_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        ad.AgenticDataBenchAdapter(tmp_path, split="lite")
