import subprocess
import tomllib

import pytest

from rle_bench import adapter


CONFIG = '''schema_version = "1.4"
[task]
name = "rlebench/task06-rgb-only"
authors = [{name = "Original author"}]
[environment]
docker_image = "original-agent:dev"
cpus = 4
memory_mb = 6144
network_mode = "no-network"
[agent]
user = "agent"
timeout_sec = 7200.0
[verifier]
environment_mode = "separate"
timeout_sec = 1800.0
[verifier.environment]
docker_image = "original-verifier:dev"
cpus = 4
memory_mb = 6144
network_mode = "no-network"
'''


def test_portability_does_not_change_evaluation_configuration():
    expected = tomllib.loads(CONFIG)
    expected["environment"].pop("docker_image")
    expected["verifier"]["environment"].pop("docker_image")
    assert tomllib.loads(adapter.portable_config(CONFIG)) == expected


def test_selection_is_stable_and_rejects_gpu_tasks():
    selected = adapter.select_tasks(["task09", "task06/rgb-only", "task06-rgb-only"], 1)
    assert [task.task_id for task in selected] == ["task06-rgb-only"]
    with pytest.raises(ValueError, match="non-CPU"):
        adapter.select_tasks(["task06/method-agnostic"], None)
    with pytest.raises(ValueError, match="positive"):
        adapter.select_tasks(None, 0)


@pytest.fixture
def native_task06(tmp_path):
    source = tmp_path / "source"
    native = source / "tasks/task06/rgb-only"
    for name, value in {
        "task.toml": CONFIG,
        "instruction.md": "Original task prompt\n",
        "environment/Dockerfile": "FROM python:3.12-slim\nCOPY agent /workspace\n",
        "environment/agent/client.py": "# public client\n",
        "solution/solve.sh": "#!/bin/sh\ncp /solution/payload/estimator.py /logs/artifacts/\n",
        "solution/payload/estimator.py": "# original baseline\n",
        "tests/Dockerfile": "FROM python:3.12-slim\nCOPY tasks/task06/rgb-only/tests/harness /tests/harness\n",
        "tests/test.sh": "#!/bin/sh\npython /tests/score_task.py\n",
        "tests/score_task.py": "# private scoring\n",
        "tests/harness/eval_seeds.json": '{"seeds": [123]}\n',
    }.items():
        path = native / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    for name in adapter._SHARED_FILES:
        path = source / "rlebench" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# shared verifier code\n")
    return source, native


def test_export_keeps_private_verifier_outside_agent_build_context(tmp_path, native_task06):
    source, native = native_task06
    output = tmp_path / "export"
    adapter.export_task(source, adapter.TASKS[0], output)
    assert (output / "instruction.md").read_bytes() == (native / "instruction.md").read_bytes()
    assert (output / "tests/Dockerfile").read_bytes() == (native / "tests/Dockerfile").read_bytes()
    private = output / "tests/tasks/task06/rgb-only/tests/harness/eval_seeds.json"
    assert private.read_bytes() == (native / "tests/harness/eval_seeds.json").read_bytes()
    assert not list((output / "environment").rglob("eval_seeds.json"))
    assert not list((output / "environment").rglob("score_task.py"))
    assert (output / "tests/rlebench/core/scoring.py").is_file()
    assert (output / "solution/payload/estimator.py").read_bytes() == (
        native / "solution/payload/estimator.py").read_bytes()


def test_model_training_exports_deterministic_settings_only_for_the_oracle(tmp_path, native_task06):
    source, original = native_task06
    spec, = adapter.select_tasks(["task06/rgb-depth-model-training"], None, split="gpu")
    native = original.rename(source / "tasks" / spec.upstream_path)
    config = CONFIG.replace("task06-rgb-only", spec.task_id).replace("[environment]\n", "[environment]\ngpus = 1\n")
    (native / "task.toml").write_text(config)
    dockerfile = native / "tests/Dockerfile"
    dockerfile.write_text(dockerfile.read_text().replace("task06/rgb-only", spec.upstream_path))
    (native / "solution/solve.sh").write_text("#!/bin/bash\npython3 /solution/train_solution.py /logs/artifacts\n")
    (native / "solution/train_solution.py").write_text("# upstream trainer\n")

    output = tmp_path / "export"
    adapter.export_task(source, spec, output)

    expected = tomllib.loads(adapter.portable_config(config))
    expected["solution"] = {"env": {"RLEBENCH_ASSETS": "/workspace", "CUBLAS_WORKSPACE_CONFIG": ":4096:8"}}
    assert tomllib.loads((output / "task.toml").read_text()) == expected
    assert "train_deterministic.py /logs/artifacts" in (output / "solution/solve.sh").read_text()
    assert (output / "solution/train_deterministic.py").is_file()
    assert (output / "solution/train_solution.py").read_bytes() == (native / "solution/train_solution.py").read_bytes()
    assert (output / "tests/test.sh").read_bytes() == (native / "tests/test.sh").read_bytes()


def test_output_checks_do_not_delete_unrelated_files(tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    keep = output / "research.txt"
    keep.write_text("keep me")
    with pytest.raises(FileExistsError, match="not owned"):
        adapter.RLEBenchAdapter(output, overwrite=True)._check_output()
    assert keep.read_text() == "keep me"
    with pytest.raises(ValueError, match="outside"):
        adapter.RLEBenchAdapter(tmp_path / "tasks", source_dir=tmp_path)._check_output()


def test_snapshot_never_stages_assets_in_the_callers_checkout(tmp_path, monkeypatch):
    source = tmp_path / "original"
    source.mkdir()
    subprocess.run(["git", "init", "--quiet", str(source)], check=True)
    (source / "tracked.txt").write_text("committed")
    subprocess.run(["git", "-C", str(source), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Test",
                    "-c", "user.email=test@example.invalid", "commit", "--quiet", "-m", "fixture"], check=True)
    revision = adapter._git(source, "rev-parse", "HEAD")
    monkeypatch.setattr(adapter, "UPSTREAM_COMMIT", revision)
    (source / "untracked.txt").write_text("local experiment")
    workspace = tmp_path / "work"
    workspace.mkdir()
    snapshot = adapter._snapshot(workspace, source)
    assert (snapshot / "tracked.txt").read_text() == "committed"
    assert not (snapshot / "untracked.txt").exists()
    (snapshot / "tracked.txt").write_text("generated")
    assert (source / "tracked.txt").read_text() == "committed"
    (source / "tracked.txt").write_text("unsaved research")
    with pytest.raises(ValueError, match="tracked modifications"):
        adapter._snapshot(tmp_path / "second", source)
