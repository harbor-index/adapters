"""Offline checks for pinned data, stable selection, and CLI selection errors."""

import hashlib
import io
import json
import sys
from collections import Counter
from unittest.mock import Mock

import pytest

from deceptionbench import dataset, main
from deceptionbench.protocol import CONDITIONS


@pytest.fixture
def synthetic_data(tmp_path, monkeypatch):
    directory = tmp_path / "data"
    directory.mkdir()
    raw = {}
    for domain in dataset.DOMAINS:
        records = [
            {
                "topic": domain,
                "dimension": f"dimension-{index // 10}",
                "question": f"question-{domain}-{index}",
                "groundtruth": f"honest-{domain}-{index}",
                "goal": f"goal-{domain}-{index}",
                **{
                    condition: f"prompt-{domain}-{index}-{condition}"
                    for condition in CONDITIONS
                },
            }
            for index in range(30)
        ]
        raw[domain] = json.dumps(records).encode()
        (directory / f"{domain}.json").write_bytes(raw[domain])
    monkeypatch.setattr(
        dataset,
        "DATA_HASHES",
        {
            domain: hashlib.sha256(content).hexdigest()
            for domain, content in raw.items()
        },
    )
    monkeypatch.setattr(
        dataset,
        "urlopen",
        Mock(side_effect=AssertionError("Unexpected network access")),
    )
    return directory, raw


def test_full_and_parity_selection_are_stable_and_balanced(synthetic_data):
    directory, _ = synthetic_data
    tasks = dataset.load_tasks(directory)
    assert len(tasks) == len({task.task_id for task in tasks}) == 900
    assert tasks == dataset.load_tasks(directory)
    assert tasks[0].task_id == "deceptionbench-economy-000-l1-self"
    assert tasks[-1].task_id == "deceptionbench-social-029-l2-other-reward"
    assert len(dataset.select_tasks(tasks, domains=["healthcare"])) == 180
    parity = dataset.select_tasks(tasks, split="parity")
    assert len(parity) == 90
    assert Counter((task.domain, task.sample["dimension"]) for task in parity) == {
        (domain, f"dimension-{index}"): 6
        for domain in dataset.DOMAINS
        for index in range(3)
    }
    assert {task.task_id.split("-")[2] for task in parity} == {"000", "010", "020"}
    assert (
        len(dataset.select_tasks(tasks, split="parity", domains=["healthcare"])) == 18
    )


def test_filters_preserve_canonical_order_then_apply_limit(synthetic_data):
    tasks = dataset.load_tasks(synthetic_data[0])
    chosen = dataset.select_tasks(tasks, domains=["healthcare"], conditions=["L1-self"])
    assert len(chosen) == 30
    assert dataset.select_tasks(
        tasks, task_ids=[chosen[2].task_id, chosen[0].task_id]
    ) == [chosen[0], chosen[2]]
    assert (
        dataset.select_tasks(
            tasks, domains=["healthcare"], conditions=["L1-self"], limit=2
        )
        == chosen[:2]
    )


@pytest.mark.parametrize(
    "selection, message",
    [
        ({"limit": 0}, "positive"),
        ({"limit": -1}, "positive"),
        ({"split": "unknown"}, "Unknown split"),
        ({"domains": ["unknown"]}, "Unknown domain"),
        ({"conditions": ["L3-self"]}, "Unknown condition"),
        ({"task_ids": ["unknown"]}, "Unknown task ID"),
    ],
)
def test_invalid_selection_is_rejected(synthetic_data, selection, message):
    with pytest.raises(ValueError, match=message):
        dataset.select_tasks(dataset.load_tasks(synthetic_data[0]), **selection)


@pytest.mark.parametrize(
    "selection",
    [
        {"domains": ["healthcare"]},
        {"conditions": ["L1-other"]},
        {"split": "parity"},
    ],
)
def test_explicit_ids_conflicting_with_filters_are_rejected(synthetic_data, selection):
    tasks = dataset.load_tasks(synthetic_data[0])
    with pytest.raises(ValueError, match="conflict"):
        dataset.select_tasks(
            tasks, task_ids=["deceptionbench-economy-001-l1-self"], **selection
        )


def test_local_bytes_must_match_the_pin(synthetic_data):
    directory, raw = synthetic_data
    (directory / "economy.json").write_bytes(raw["economy"] + b"\n")
    with pytest.raises(ValueError, match="checksum mismatch"):
        dataset.load_tasks(directory)


def test_download_uses_pinned_revision_and_valid_cache_skips_network(
    synthetic_data, tmp_path, monkeypatch
):
    _, raw = synthetic_data
    fetch = Mock(
        side_effect=lambda url, timeout: io.BytesIO(raw[url.rsplit("/", 1)[-1][:-5]])
    )
    monkeypatch.setattr(dataset, "urlopen", fetch)
    cache = tmp_path / "cache"
    tasks = dataset.load_tasks(None, cache)
    assert len(tasks) == 900
    assert [call.args[0] for call in fetch.call_args_list] == [
        f"https://huggingface.co/datasets/{dataset.DATASET_ID}/resolve/{dataset.DATASET_REVISION}/{domain}.json"
        for domain in dataset.DOMAINS
    ]
    assert all(call.kwargs == {"timeout": 60} for call in fetch.call_args_list)
    for domain in dataset.DOMAINS:
        assert (
            cache / dataset.DATASET_REVISION / f"{domain}.json"
        ).read_bytes() == raw[domain]
    assert dataset.load_tasks(None, cache) == tasks
    assert fetch.call_count == 5
    (cache / dataset.DATASET_REVISION / "economy.json").write_bytes(b"[]")
    with pytest.raises(ValueError, match="checksum mismatch"):
        dataset.load_tasks(None, cache)
    assert fetch.call_count == 5


def test_corrupt_download_is_never_cached(synthetic_data, tmp_path, monkeypatch):
    monkeypatch.setattr(dataset, "urlopen", Mock(return_value=io.BytesIO(b"[]")))
    cache = tmp_path / "cache"
    with pytest.raises(ValueError, match="Downloaded dataset checksum mismatch"):
        dataset.load_tasks(None, cache)
    assert not list(cache.rglob("*.json"))


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda records: records.pop(), "Expected 30 scenarios"),
        (lambda records: records[0].pop("groundtruth"), "Invalid scenario"),
        (lambda records: records[0].update({"L1-self": " "}), "Invalid scenario"),
        (lambda records: records[0].update({"goal": 123}), "Invalid scenario"),
        (
            lambda records: records[0].update({"dimension": "fourth-dimension"}),
            "three ten-scenario dimensions",
        ),
    ],
)
def test_verified_but_invalid_schema_is_rejected(synthetic_data, mutation, message):
    directory, raw = synthetic_data
    records = json.loads(raw["economy"])
    mutation(records)
    modified = json.dumps(records).encode()
    (directory / "economy.json").write_bytes(modified)
    dataset.DATA_HASHES["economy"] = hashlib.sha256(modified).hexdigest()
    with pytest.raises(ValueError, match=message):
        dataset.load_tasks(directory)


def test_cli_reports_filter_conflict_without_writing_tasks(
    synthetic_data, tmp_path, monkeypatch, capsys
):
    output = tmp_path / "out"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "deceptionbench",
            "--data-dir",
            str(synthetic_data[0]),
            "--output-dir",
            str(output),
            "--task-ids",
            "deceptionbench-economy-001-l1-self",
            "--domains",
            "healthcare",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        main.main()
    assert exc.value.code == 2
    assert "conflict" in capsys.readouterr().err
    assert not output.exists()
