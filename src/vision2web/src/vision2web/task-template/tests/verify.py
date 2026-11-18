"""Draft verifier: clean deployment, unchanged upstream GUI/VLM prompts and scoring.

Capture-only mode never writes a reward. Model calls require explicit enablement
and separate judge credentials. Reference sites receive no judge environment.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import pwd
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from metrics import task_reward

ROOT = Path(__file__).parent
WORKSPACE = Path("/workspace")
LOGS = Path("/logs/verifier")
RESULTS = LOGS / "evaluation"
QA = LOGS / "qa"
URL = "http://localhost:3000"


def settings(task: dict) -> dict:
    if os.environ.get("VISION2WEB_ENABLE_MODEL_GRADING") != "1":
        raise RuntimeError(
            "Model grading is disabled. Coordinate judge access/oracle acceptance; "
            "set VISION2WEB_ENABLE_MODEL_GRADING=1 only for approved runs."
        )
    result = {
        name: os.environ.get("VISION2WEB_" + name, "")
        for name in [
            "VISUAL_MODEL",
            "VISUAL_API_KEY",
            "VISUAL_BASE_URL",
            "FUNCTIONAL_MODEL",
            "FUNCTIONAL_API_KEY",
            "FUNCTIONAL_BASE_URL",
        ]
    }
    result["VISUAL_MODEL"] = result["VISUAL_MODEL"] or "gemini-3-pro-preview"
    result["FUNCTIONAL_MODEL"] = result["FUNCTIONAL_MODEL"] or "claude-sonnet-4-5-20250929"
    if not result["VISUAL_API_KEY"] or not result["VISUAL_BASE_URL"]:
        raise RuntimeError("Set the visual judge key and OpenAI-compatible base URL explicitly")
    if task["level"] != "webpage" and not result["FUNCTIONAL_API_KEY"]:
        raise RuntimeError("Set the functional judge key for L2/L3")
    return result


def prepare_outputs():
    LOGS.mkdir(parents=True, exist_ok=True)
    LOGS.chmod(0o700)
    for filename in ["reward.txt", "reward.json", "metrics.json"]:
        (LOGS / filename).unlink(missing_ok=True)
    for p in [RESULTS, QA]:
        if p.exists():
            shutil.rmtree(p)
        p.mkdir(mode=0o700)
    # These never originate in a submitted site.
    Path("/tests").chmod(0o700)


def deploy() -> tuple[subprocess.Popen, object]:
    if not (WORKSPACE / "start.sh").is_file():
        raise FileNotFoundError("Website did not deliver /workspace/start.sh")
    account = pwd.getpwnam("website")
    # Do not follow untrusted symlinks while changing ownership.
    for p in [WORKSPACE, *WORKSPACE.rglob("*")]:
        os.chown(p, account.pw_uid, account.pw_gid, follow_symlinks=False)
    log = (LOGS / "deploy.log").open("wb")
    # Fresh site process has a distinct UID and a minimal, credential-free environment.
    proc = subprocess.Popen(
        [
            "runuser",
            "-u",
            "website",
            "--",
            "env",
            "-i",
            "HOME=/home/website",
            "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LANG=en_US.UTF-8",
            "TZ=UTC",
            "bash",
            "start.sh",
        ],
        cwd=WORKSPACE,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return proc, log


async def wait_for_port(proc, timeout=600):
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        check = await asyncio.create_subprocess_exec(
            "curl", "-s", URL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await check.communicate()
        if check.returncode == 0 and stdout.strip():
            return True
        if proc.poll() is not None:
            return False
        await asyncio.sleep(3)
    return False


async def command(args, name, env=None):
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=QA, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    (LOGS / (name + ".stdout")).write_bytes(stdout)
    (LOGS / (name + ".stderr")).write_bytes(stderr)
    return proc.returncode


async def capture_static(workflow, index):
    resolution = workflow.get("resolution", {})
    width, height = resolution.get("width", 1024), resolution.get("height", 768)
    target = RESULTS / "test_results/prototypes"
    target.mkdir(parents=True, exist_ok=True)
    lines = [
        "rc=0",
        f"playwright-cli open {URL} || exit 1",
        f"playwright-cli resize {width} {height} || exit 1",
    ]
    for name, conf in workflow.get("prototype", {}).items():
        flag = "--full-page " if conf.get("fullpage", True) else ""
        filename = shlex.quote(str(target / (name + "_actual.png")))
        lines.append(f"playwright-cli screenshot {flag}--filename={filename} || rc=1")
    lines += ["playwright-cli close || true", "exit $rc"]
    return await command(["bash", "-c", "\n".join(lines)], f"screenshot-{index}")


async def evaluate(task, workflows, config):
    # Import the original pinned package in the generated verifier, not the adapter package.
    sys.path.insert(0, str(ROOT / "upstream"))
    from vision2web.core.utils import build_claude_code_env
    from vision2web.evaluation.functional_tester import FunctionalTester
    from vision2web.evaluation.visual_scorer import VisualScorer

    tester = FunctionalTester(
        model=config["FUNCTIONAL_MODEL"],
        api_key=config["FUNCTIONAL_API_KEY"],
        base_url=config["FUNCTIONAL_BASE_URL"] or None,
    )
    visual = VisualScorer(
        api_key=config["VISUAL_API_KEY"],
        base_url=config["VISUAL_BASE_URL"],
        visual_model=config["VISUAL_MODEL"],
    )
    events = []
    for index, workflow in enumerate(workflows):
        if not workflow.get("content") and workflow.get("prototype"):
            code = await capture_static(workflow, index)
        else:
            prompt = tester._build_workflow_prompt(URL, workflow, index, str(RESULTS))
            (LOGS / f"workflow-{index}-prompt.txt").write_text(prompt)
            env = dict(os.environ)
            env.update(
                {
                    k: str(v)
                    for k, v in build_claude_code_env(
                        config["FUNCTIONAL_BASE_URL"] or None,
                        config["FUNCTIONAL_API_KEY"],
                        config["FUNCTIONAL_MODEL"],
                    ).items()
                }
            )
            code = await command(
                [
                    "claude",
                    "--print",
                    "--model",
                    config["FUNCTIONAL_MODEL"],
                    "--dangerously-skip-permissions",
                    "-p",
                    prompt,
                ],
                f"functional-{index}",
                env,
            )
        # Files are already local, so no Docker copy-back is needed.
        result = tester._collect_results(RESULTS, index, workflow)
        scores = await visual.score_all_prototypes(
            workflow_item=workflow,
            workflow_idx=index,
            dataset_path=str(ROOT / "data" / task["level"] / task["task_name"]),
            output_dir=RESULTS,
            actual_pages=result["screenshots"],
        )
        events.append(
            {
                "workflow": index,
                "runner_exit": code,
                "captured": list(result["screenshots"]),
                "scored": list(scores),
            }
        )
    return events


def read_original_scores(task, success):
    sys.path.insert(0, str(ROOT / "upstream"))
    from vision2web.analyze.analyzer import ResultAnalyzer

    scorer = ResultAnalyzer()
    args = (RESULTS, task["level"], task["task_name"], success, ROOT / "data")
    return {
        "id": task["id"],
        "level": task["level"],
        "success": success,
        "prototypes": scorer._get_prototype_scores(*args),
        "function": scorer._get_function_scores(*args),
    }


async def main(capture_only=False):
    task = json.loads((ROOT / "task.json").read_text())
    project = ROOT / "data" / task["level"] / task["task_name"]
    workflows = json.loads((project / "workflow.json").read_text())
    LOGS.mkdir(parents=True, exist_ok=True)
    for filename in ["reward.txt", "reward.json"]:
        (LOGS / filename).unlink(missing_ok=True)
    config = None if capture_only else settings(task)  # Fail before launching a site or model.
    prepare_outputs()
    proc, log = deploy()
    try:
        ready = await wait_for_port(proc)
        if capture_only:
            captures = []
            if ready:
                for index, wf in enumerate(workflows):
                    # Only nodes at initial page load can be captured without GUI actions.
                    initial = {
                        k: v
                        for k, v in wf.get("prototype", {}).items()
                        if not wf.get("content") or v.get("idx", 0) == 0
                    }
                    if initial:
                        code = await capture_static(
                            {**wf, "content": [], "prototype": initial}, index
                        )
                        captures.append(
                            {"workflow": index, "names": list(initial), "exit_code": code}
                        )
            receipt = {
                "mode": "capture-only",
                "task": task["id"],
                "deployed": ready,
                "captures": captures,
                "benchmark_reward": None,
                "model_calls": 0,
            }
            (LOGS / "capture.json").write_text(json.dumps(receipt, indent=2) + "\n")
            if not ready or any(c["exit_code"] for c in captures):
                raise RuntimeError("Capture-only deployment/screenshot failed; inspect logs")
            return
        events = await evaluate(task, workflows, config) if ready else []
        record = read_original_scores(task, ready)
        record["workflow_events"] = events
        record["judge_models"] = {k: v for k, v in config.items() if k.endswith("_MODEL")}
        (LOGS / "metrics.json").write_text(json.dumps(record, indent=2) + "\n")
        (LOGS / "reward.txt").write_text(str(task_reward(record)) + "\n")
    finally:
        # All processes live in this disposable verifier container.
        if proc.poll() is None:
            proc.terminate()
        log.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-only", action="store_true")
    asyncio.run(main(parser.parse_args().capture_only))
