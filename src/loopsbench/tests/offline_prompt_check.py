"""Run with Harbor 0.27.0's Python. Capture both Codex command arguments offline."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock

import yaml
from harbor.agents.installed.codex import Codex
from harbor.models.task.task import Task


async def check(plan, checkout):
    job = yaml.safe_load((plan / "harbor.yaml").read_text())
    config, = job["agents"]
    paths = [Path(t["path"]) for t in job["tasks"]]
    sources = []
    for task in paths:
        native = yaml.safe_load((plan / task.name / "run.yaml").read_text())
        sources.append(Path(native["dataset_path"]) / native["task_ids"][0])
    helper = Path(__file__).resolve().parents[1] / "src/loopsbench/parity_prompts.py"
    expected = json.loads(subprocess.check_output([
        checkout / ".venv/bin/python", "-I", helper, "--capture", *sources]))
    with TemporaryDirectory() as tmp:
        agent = Codex(logs_dir=Path(tmp), model_name=config["model_name"],
                      base_url=config["base_url"], **config["kwargs"])
        agent._ensure_agent_owned_dir = AsyncMock(return_value=(1000, 1000))
        agent._upload_effective_config = AsyncMock()
        agent.exec_as_root = AsyncMock()
        agent.exec_as_agent = AsyncMock()
        for path, upstream in zip(paths, expected, strict=True):
            agent.exec_as_agent.reset_mock()
            await agent.run(Task(path).instruction, Mock(), Mock())
            command, = [call.kwargs["command"] for call in agent.exec_as_agent.await_args_list
                        if "codex exec " in call.kwargs["command"]]
            argv = shlex.split(command)
            harbor = argv[argv.index("--") + 1].encode("utf8")
            if harbor != upstream.encode("utf8"):
                raise ValueError(f"Codex instruction differs for {path.name}")
            print(path.name, len(harbor), hashlib.sha256(harbor).hexdigest())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("checkout", type=Path)
    args = parser.parse_args()
    asyncio.run(check(args.plan.resolve(), args.checkout.resolve()))
