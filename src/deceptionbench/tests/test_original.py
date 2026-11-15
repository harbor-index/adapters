"""Offline checks for the independent upstream runner and its failure ledger."""

import hashlib
import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from deceptionbench import original


@pytest.fixture
def task():
    return SimpleNamespace(
        task_id="healthcare-001-l2-self-reward",
        condition="L2-self-reward",
        sample={"question": "synthetic question", "L2-self-reward": "synthetic prompt"},
    )


def harness(response="synthetic response", labels=None):
    generation = {
        "responses": {"L2-self-reward": response},
        "timestamp": "2026-11-14 10:00:00",
    }
    if labels is None:
        labels = {"thought": "decept", "response": "honest"}
    return SimpleNamespace(
        single_turn=SimpleNamespace(process_sample=Mock(return_value=generation)),
        eval_single_turn=SimpleNamespace(
            process_sample=Mock(return_value={"eval": {"L2-self-reward": labels}})
        ),
    )


def test_runs_real_upstream_entry_points_without_rebuilding_records(task):
    upstream = harness()
    model_client, judge_client = object(), object()
    record = original.run_task(
        task, upstream, model_client, judge_client, "model", "judge"
    )
    upstream.single_turn.process_sample.assert_called_once_with(
        task.sample,
        model_client,
        {**original.MODEL_PARAMS, "model": "model"},
        levels=["L2"],
        roles=["self"],
        inducements=["reward"],
    )
    upstream.eval_single_turn.process_sample.assert_called_once_with(
        record["generation"],
        judge_client,
        {**original.MODEL_PARAMS, "model": "judge"},
        levels=["L2"],
        roles=["self"],
        inducements=["reward"],
    )
    assert record["status"] == "scored"
    assert record["reward"] == 1
    assert record["thought_deception"] == 1
    assert record["response_deception"] == 0


def test_l1_has_no_inducement(task):
    task.condition = "L1-other"
    upstream = harness()
    original.run_task(task, upstream, object(), object(), "model", "judge")
    assert upstream.single_turn.process_sample.call_args.kwargs == {
        "levels": ["L1"],
        "roles": ["other"],
        "inducements": [],
    }


@pytest.mark.parametrize("response", [None, "", "   ", "Error: API request failed"])
def test_failed_model_is_recorded_and_not_judged(task, response):
    upstream = harness(response=response)
    record = original.run_task(task, upstream, object(), object(), "model", "judge")
    assert record["status"] == "model_error"
    assert "reward" not in record
    upstream.eval_single_turn.process_sample.assert_not_called()


@pytest.mark.parametrize(
    "labels",
    [{}, {"thought": "honest"}, {"thought": "honest", "response": "unknown"}, []],
)
def test_malformed_judge_result_is_not_assigned_a_fake_score(task, labels):
    record = original.run_task(
        task, harness(labels=labels), object(), object(), "model", "judge"
    )
    assert record["status"] == "judge_error"
    assert "reward" not in record
    assert "evaluation" in record


def test_load_upstream_does_not_execute_until_every_file_is_verified(
    tmp_path, monkeypatch
):
    marker = tmp_path / "executed"
    first = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n".encode()
    (tmp_path / "first.py").write_bytes(first)
    (tmp_path / "second.py").write_bytes(b"changed")
    monkeypatch.setattr(
        original,
        "UPSTREAM_HASHES",
        {
            "first.py": hashlib.sha256(first).hexdigest(),
            "second.py": "0" * 64,
        },
    )
    with pytest.raises(ValueError, match="checksum mismatch"):
        original.load_upstream(tmp_path)
    assert not marker.exists()


def test_load_upstream_resolves_bare_imports_and_restores_modules(
    tmp_path, monkeypatch
):
    sources = {
        "utils.py": b"value = 7\n",
        "data_loader.py": b"from utils import value\n",
        "processor/single_turn.py": b"from data_loader import value\n",
    }
    for name, content in sources.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    sentinel = SimpleNamespace(value=999)
    monkeypatch.setitem(sys.modules, "utils", sentinel)
    monkeypatch.setattr(
        original,
        "UPSTREAM_HASHES",
        {
            name: hashlib.sha256(content).hexdigest()
            for name, content in sources.items()
        },
    )
    loaded = original.load_upstream(tmp_path)
    assert loaded.single_turn.value == 7
    assert sys.modules["utils"] is sentinel


def test_cli_records_each_failure_and_continues(tmp_path, monkeypatch, task):
    second = SimpleNamespace(**{**vars(task), "task_id": "second-task"})
    monkeypatch.setitem(
        sys.modules,
        "deceptionbench.dataset",
        SimpleNamespace(
            load_tasks=lambda *_: [task, second],
            select_tasks=lambda tasks, **_: tasks,
        ),
    )
    client = Mock()
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=client))
    monkeypatch.setattr(original, "load_upstream", lambda _: object())
    monkeypatch.setattr(original.importlib.metadata, "version", lambda _: "2.54.0")
    runner = Mock(
        side_effect=[
            RuntimeError("sensitive diagnostic"),
            {"task_id": second.task_id, "status": "scored", "reward": 1.0},
        ]
    )
    monkeypatch.setattr(original, "run_task", runner)
    monkeypatch.setenv("DECEPTIONBENCH_MODEL_API_KEY", "model-test-key")
    monkeypatch.setenv("DECEPTIONBENCH_JUDGE_API_KEY", "judge-test-key")
    monkeypatch.delenv("DECEPTIONBENCH_MODEL_BASE_URL", raising=False)
    monkeypatch.delenv("DECEPTIONBENCH_JUDGE_BASE_URL", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "original",
            "--upstream-dir",
            str(tmp_path),
            "--output-dir",
            str(tmp_path / "out"),
            "--model",
            "model",
        ],
    )
    with pytest.raises(SystemExit, match="1/2 trials unscored"):
        original.main()
    records = [
        json.loads(line)
        for line in (tmp_path / "out/results.jsonl").read_text().splitlines()
    ]
    assert records[0] == {
        "task_id": task.task_id,
        "condition": task.condition,
        "status": "runner_error",
        "error_type": "RuntimeError",
    }
    assert records[1]["task_id"] == second.task_id
    assert records[1]["reward"] == 1
    assert [call.kwargs["api_key"] for call in client.call_args_list] == [
        "model-test-key",
        "judge-test-key",
    ]
    assert all(call.kwargs["max_retries"] == 2 for call in client.call_args_list)
