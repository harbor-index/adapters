"""Run the upstream NoteWebsiteAgent on one pinned, freshly initialized Note site."""
import argparse
import asyncio
import filecmp
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from pinned import load_upstream


async def run(args):
    policy_class = load_upstream()
    from environment_manager import EnvironmentManager
    from playwright_controller import PlaywrightController

    task = args.task.resolve()
    query = json.loads((task / "environment/browser/query.json").read_text())["query"]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    site = output / "website"
    shutil.copytree(task / "environment/browser/site", site)
    for section in ("frontend", "backend"):
        target = site / section
        if args.deps:
            dep = args.deps.resolve() / section
            if not filecmp.cmp(dep/"package-lock.json", target/"package-lock.json", shallow=False):
                raise ValueError(f"Different lockfile for {section}")
            (target/"node_modules").symlink_to(dep/"node_modules", target_is_directory=True)
        else:
            subprocess.run(["npm", "ci", "--legacy-peer-deps", "--no-audit", "--no-fund"],
                           cwd=target, check=True)
    manager = EnvironmentManager(str(site))
    controller = PlaywrightController(headless=True)
    try:
        url, _, _ = await manager.start()
        await controller.initialize()
        result_dir = output / "original"
        agent = policy_class(controller, model=os.environ["STRESSWEB_MODEL"], max_steps=100,
                             use_dom=True, result_dir=str(result_dir))
        result = await agent.run_task(query, url)
        result["query"] = query
        (result_dir/"result.json").write_text(json.dumps(result, indent=2))
        if result.get("message", "").startswith("API call failed:"):
            raise RuntimeError("Model API failed; see the upstream run output")
        evaluator = subprocess.run([sys.executable, str(task/"tests/evaluator.py"), str(result_dir)],
                                   text=True, capture_output=True)
        if evaluator.returncode not in (0, 1):
            raise RuntimeError(f"Upstream evaluator failed: {evaluator.stderr}")
        measured = json.loads(evaluator.stdout)
        (output/"checkpoints.json").write_text(json.dumps(measured, indent=2) + "\n")
    finally:
        await controller.close()
        await manager.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--deps", type=Path)
    asyncio.run(run(parser.parse_args()))
