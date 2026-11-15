"""Generated-task isolation, deterministic rendering, and safe replacement."""

import base64
import hashlib
import json
import re
import stat
import tomllib

import pytest

from deceptionbench import adapter
from deceptionbench.dataset import SampleTask
from deceptionbench.protocol import CONDITIONS, RESPONSE_SUFFIX, build_generation_prompt


@pytest.fixture
def tasks(monkeypatch):
    sample = {
        "topic": "healthcare",
        "dimension": "synthetic dimension",
        "question": "PRIVATE_QUESTION",
        "groundtruth": "PRIVATE_GOLD_\"\n'答案",
        "goal": "PRIVATE_GOAL",
        **{condition: f"PUBLIC_PROMPT_{condition}" for condition in CONDITIONS},
    }
    tasks = [
        SampleTask(
            f"deceptionbench-healthcare-000-{condition.lower()}",
            "healthcare",
            condition,
            sample,
        )
        for condition in ("L1-self", "L2-self-reward")
    ]
    monkeypatch.setattr(adapter, "load_tasks", lambda *_: tasks)
    monkeypatch.setattr(adapter, "DATA_HASHES", {"healthcare": "1" * 64})
    return tasks


def tree_hashes(directory):
    return {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.rglob("*")
        if path.is_file()
    }


def test_generation_is_deterministic_and_overwrite_replaces_only_owned_tasks(
    tmp_path, tasks
):
    output = tmp_path / "first"
    generated = adapter.DeceptionBenchAdapter(output).run()
    assert generated == [output / task.task_id for task in tasks]
    expected = tree_hashes(output)
    other_output = tmp_path / "second"
    adapter.DeceptionBenchAdapter(other_output).run()
    assert tree_hashes(other_output) == expected
    (generated[0] / "instruction.md").write_text("changed")
    (generated[0] / "stale.txt").write_text("old file")
    unrelated = output / "keep.txt"
    unrelated.write_text("user file")
    adapter.DeceptionBenchAdapter(output, overwrite=True).run()
    assert tree_hashes(output) == {
        **expected,
        "keep.txt": hashlib.sha256(b"user file").hexdigest(),
    }
    assert not list(output.glob(".deceptionbench-*"))


def test_existing_tasks_require_overwrite(tmp_path, tasks):
    adapter.DeceptionBenchAdapter(tmp_path).run()
    before = tree_hashes(tmp_path)
    with pytest.raises(FileExistsError, match="overwrite"):
        adapter.DeceptionBenchAdapter(tmp_path).run()
    assert tree_hashes(tmp_path) == before


def test_overwrite_preflights_every_destination_before_mutating_any(tmp_path, tasks):
    adapter.DeceptionBenchAdapter(tmp_path, limit=1).run()
    unrelated = tmp_path / tasks[1].task_id
    unrelated.mkdir()
    (unrelated / "task.toml").write_text('[task]\nname = "somebody-else/task"\n')
    (unrelated / "keep.txt").write_text("user data")
    before = tree_hashes(tmp_path)
    with pytest.raises(ValueError, match="unrelated directory"):
        adapter.DeceptionBenchAdapter(tmp_path, overwrite=True).run()
    assert tree_hashes(tmp_path) == before


def test_symlink_destination_is_refused(tmp_path, tasks):
    output = tmp_path / "output"
    output.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("user data")
    (output / tasks[0].task_id).symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        adapter.DeceptionBenchAdapter(output, limit=1, overwrite=True).run()
    assert (target / "keep.txt").read_text() == "user data"
    assert (output / tasks[0].task_id).is_symlink()


def test_render_failure_preserves_existing_task(tmp_path, tasks, monkeypatch):
    adapter.DeceptionBenchAdapter(tmp_path, limit=1).run()
    before = tree_hashes(tmp_path)
    generator = adapter.DeceptionBenchAdapter(tmp_path, limit=1, overwrite=True)

    def fail_render(*_):
        raise OSError("synthetic failure")

    monkeypatch.setattr(generator, "_render", fail_render)
    with pytest.raises(OSError, match="synthetic failure"):
        generator.run()
    assert tree_hashes(tmp_path) == before
    assert not list(tmp_path.glob(".deceptionbench-*"))


def test_references_are_hidden_and_selected_prompt_is_preserved(tmp_path, tasks):
    path = adapter.DeceptionBenchAdapter(tmp_path, limit=1).run()[0]
    task = tasks[0]
    instruction = (path / "instruction.md").read_text()
    assert instruction == build_generation_prompt(task.sample[task.condition]) + "\n"
    environment = path / "environment"
    assert {item.name for item in environment.iterdir()} == {"Dockerfile", "prompt.txt"}
    assert (environment / "prompt.txt").read_text() == task.sample[
        task.condition
    ] + RESPONSE_SUFFIX
    exposed = instruction + "".join(item.read_text() for item in environment.iterdir())
    for value in (
        task.sample["question"],
        task.sample["groundtruth"],
        task.sample["goal"],
    ):
        assert value not in exposed
    for condition in CONDITIONS:
        if condition != task.condition:
            assert task.sample[condition] not in exposed
    dockerfile = (environment / "Dockerfile").read_text()
    assert "COPY prompt.txt /workspace/prompt.txt" in dockerfile
    assert "/tests" not in dockerfile and "/solution" not in dockerfile
    metadata = json.loads((path / "tests/metadata.json").read_text())
    assert metadata["sample"] == task.sample
    assert metadata["provenance"] == {
        "upstream_commit": adapter.UPSTREAM_COMMIT,
        "dataset": adapter.DATASET_ID,
        "dataset_revision": adapter.DATASET_REVISION,
        "dataset_file": "healthcare.json",
        "dataset_sha256": "1" * 64,
    }
    oracle_script = (path / "solution/solve.sh").read_text()
    encoded = re.search(
        r'base64\.b64decode\("([A-Za-z0-9+/=]+)"\)', oracle_script
    ).group(1)
    assert base64.b64decode(encoded).decode() == task.sample["groundtruth"]
    for script in (path / "solution/solve.sh", path / "tests/test.sh"):
        assert script.stat().st_mode & stat.S_IXUSR


def test_task_config_separates_judge_credentials_and_collects_response(tmp_path, tasks):
    paths = adapter.DeceptionBenchAdapter(tmp_path).run()
    for path, task in zip(paths, tasks, strict=True):
        config = tomllib.loads((path / "task.toml").read_text())
        assert config["task"]["name"] == f"aries-iai/{task.task_id}"
        assert config["metadata"]["condition"] == task.condition
        assert config["verifier"]["environment_mode"] == "separate"
        assert set(config["verifier"]["env"]) == {
            "DECEPTIONBENCH_JUDGE_API_KEY",
            "DECEPTIONBENCH_JUDGE_BASE_URL",
            "DECEPTIONBENCH_JUDGE_MODEL",
        }
        assert "env" not in config["agent"]
        assert "env" not in config["environment"]
        assert config["artifacts"] == [
            {"source": "/workspace/response.txt", "destination": "response.txt"}
        ]
        assert "OPENAI_API_KEY" not in (path / "task.toml").read_text()
