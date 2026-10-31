from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

import adapter

# Mixed case, as in the Lite split: the published image name is lower-cased.
INSTANCE_ID = "Project-MONAI__MONAI-1121"
IMAGE = "xingyaoww/sweb.eval.x86_64.project-monai_s_monai-1121"
RECORD = {
    "instance_id": INSTANCE_ID,
    "repo": "Project-MONAI/MONAI",
    "version": "0.3",
    "base_commit": "deadbeef",
    "problem_statement": "Fix the failing test.",
    "difficulty": "hard",
    "patch": "",
    "test_patch": "",
    "FAIL_TO_PASS": [],
    "PASS_TO_PASS": [],
}


def test_task_config_names_the_dockerfile_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only the Hugging Face download is replaced; the real loader runs.
    monkeypatch.setattr(adapter, "load_dataset", lambda name: {"train": [RECORD]})
    converter = adapter.SWEGymToHarbor(harbor_tasks_root=tmp_path)

    task_dir = converter.generate_task(INSTANCE_ID, INSTANCE_ID.lower())

    config = tomllib.loads((task_dir / "task.toml").read_text())
    assert config["task"]["name"] == f"swe-gym/swe-gym__{INSTANCE_ID}"
    assert config["task"]["authors"][0]["name"] == "Jiayi Pan"
    environment = config["environment"]
    assert environment["docker_image"] == IMAGE
    assert environment["workdir"] == "/testbed"
    dockerfile = (task_dir / "environment" / "Dockerfile").read_text()
    assert dockerfile.splitlines()[0] == f"FROM {IMAGE}"
    assert f"WORKDIR {environment['workdir']}" in dockerfile.splitlines()
