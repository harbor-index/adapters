"""Prepare paired jobs for one agreed parity run without launching either harness."""

import argparse
import json
import subprocess
import sys
from pathlib import Path
import re
import shutil
import tomllib
from urllib.parse import urlsplit

import yaml

from .adapter import harbor_id
from .main import PARITY_TASK_IDS
from .verifier_network import prepare_upstream

SMOKE_IDS = (
    "distributed-fs-chfs", "evosuite-test-generation", "navidrome-seg02",
    "numpy-dl-aics-style", "cocos2dx-physics2d-medium",
)


def native_prompts(checkout, directories):
    commit = subprocess.check_output(["git", "-C", checkout, "rev-parse", "HEAD"], text=True).strip()
    if commit != "f44331c1b41a37a84e33c8f11bcc3648d96b12ed":
        raise ValueError("Expected upstream checkout f44331c")
    helper = Path(__file__).resolve().with_name("parity_prompts.py")
    result = subprocess.check_output([checkout.resolve() / ".venv/bin/python", "-I", helper,
                                      *directories], cwd=checkout)
    return json.loads(result)


def check_version(plan):
    job = yaml.safe_load((plan / "harbor.yaml").read_text())
    version = job["agents"][0]["kwargs"]["version"]
    binaries = set()
    for task in job["tasks"]:
        path = plan / Path(task["path"]).name / "run.yaml"
        cfg = yaml.safe_load(path.read_text())
        binaries.add(Path(cfg["agent_kwargs"]["codex_binary"]))
    if len(binaries) != 1:
        raise ValueError("The native jobs must share one Codex binary")
    binary = binaries.pop()
    with binary.open("rb") as stream:
        if stream.read(4) != b"\x7fELF":
            raise ValueError(f"{binary} must be a native Linux executable")
    actual = subprocess.check_output([binary, "--version"], text=True).strip()
    if actual != f"codex-cli {version}":
        raise ValueError(f"{binary} reports {actual!r}; plan requires {version}")


def prepare(source: Path, harbor: Path, output: Path, *, split: str, model: str,
            base_url: str, version: str, binary: Path, checkout: Path) -> None:
    endpoint = urlsplit(base_url)
    if (endpoint.scheme != "https" or not endpoint.hostname or endpoint.username is not None
            or endpoint.password is not None or endpoint.query or endpoint.fragment):
        raise ValueError("Supply a container-reachable HTTPS base URL without credentials")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[\w.-]+)?", version):
        raise ValueError("Supply an exact Codex CLI release")
    if not model.startswith("openai/") or not model.removeprefix("openai/").strip():
        raise ValueError("Supply the agreed openai/MODEL name")
    tasks = {"smoke": SMOKE_IDS, "full": PARITY_TASK_IDS}[split]
    output = output.resolve()
    if any(output.is_relative_to(path.resolve()) for path in (source, harbor)):
        raise ValueError("Use a new output directory outside the inputs")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    try:
        harbor_tasks = []
        staged_sources = []
        for task_id in tasks:
            task = (harbor / task_id).resolve()
            meta = tomllib.loads((task / "task.toml").read_text())["metadata"]
            upstream_id = meta["upstream_task_id"]
            if meta["upstream_bundle"] != "c47e13a" or (
                Path(upstream_id).name != upstream_id or harbor_id(upstream_id) != task_id
            ):
                raise ValueError(f"{task}: unexpected source metadata")
            path = prepare_upstream(source / upstream_id, task, output / task_id,
                                    model_host=endpoint.hostname)
            native = yaml.safe_load(path.read_text())
            native.update(agent="codex", model=model, n_attempts=1, n_concurrent_trials=1,
                          global_timeout_multiplier=1.0, agent_kwargs={
                              "base_url": base_url, "api_key_env": "OPENAI_API_KEY",
                              "codex_binary": str(binary.resolve()), "outer_loop_count": 1,
                          })
            path.write_text(yaml.safe_dump(native, sort_keys=False))
            frozen = output / "harbor-tasks" / task_id
            shutil.copytree(task, frozen, symlinks=True)
            staged_sources.append(Path(native["dataset_path"]) / upstream_id)
            harbor_tasks.append({"path": str(frozen)})
        prompts = native_prompts(checkout, staged_sources)
        for entry, prompt in zip(harbor_tasks, prompts, strict=True):
            (Path(entry["path"]) / "instruction.md").write_bytes(prompt.encode("utf-8"))
        job = {
            "jobs_dir": str(output / "harbor-results"), "n_attempts": 1,
            "n_concurrent_trials": 1, "timeout_multiplier": 1.0, "retry": {"max_retries": 0},
            "environment": {"type": "docker", "force_build": True, "delete": True},
            "agents": [{"name": "codex", "model_name": model, "base_url": base_url,
                        "model_api_key_name": "OPENAI_API_KEY",
                        "extra_allowed_hosts": [endpoint.hostname],
                        "kwargs": {"version": version, "web_search": "disabled",
                                   "config": {"features": {"goals": True}}}}],
            "tasks": harbor_tasks,
        }
        (output / "harbor.yaml").write_text(yaml.safe_dump(job, sort_keys=False))
    except BaseException:
        shutil.rmtree(output)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    if sys.argv[1:2] == ["check-version"]:
        parser.add_argument("plan", type=Path)
        check_version(parser.parse_args(sys.argv[2:]).plan)
        return
    for name in ("source", "harbor", "output", "binary", "checkout"):
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--split", choices=("smoke", "full"), required=True)
    for name in ("model", "base-url", "version"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    prepare(**vars(args))


if __name__ == "__main__":
    main()
