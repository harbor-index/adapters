"""Check that configs/harbor_mini_swe_agent.yaml reproduces the upstream Mini-SWE-Agent prompt.

For every generated task, renders the Harbor-side templates with `{{task}}` =
instruction.md (what Harbor's mini-swe-agent passes as --task) and the upstream
templates with `{{task}}` = distdebug_bench.agent.build_task(...), using jinja2
with StrictUndefined exactly as minisweagent.agents.default does, and compares
the system and instance messages byte for byte.

Usage (needs jinja2 and pyyaml, plus an upstream checkout):
    uv run --with jinja2 python scripts/check_mini_prompt.py \
        --upstream /path/to/distdebug-bench --tasks /path/to/datasets/distdebug-bench
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

import yaml
from jinja2 import StrictUndefined, Template

HERE = Path(__file__).resolve().parent
SYSINFO = {"system": "Linux", "release": "6.8.0", "version": "#1 SMP", "machine": "x86_64"}


def render(template: str, task: str) -> str:
    return Template(template, undefined=StrictUndefined).render(task=task, **SYSINFO)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=HERE.parent / "configs" / "harbor_mini_swe_agent.yaml")
    args = parser.parse_args()

    sys.path.insert(0, str(args.upstream.resolve()))
    from distdebug_bench import load_benchmark
    from distdebug_bench.agent import PROMPT_CONFIG, build_task, stage_workspace

    upstream = yaml.safe_load(PROMPT_CONFIG.read_text())["agent"]
    harbor = yaml.safe_load(args.config.read_text())["agent"]
    bench = load_benchmark(args.upstream)
    failures = 0
    for task_dir in sorted(p for p in args.tasks.iterdir() if (p / "instruction.md").exists()):
        bug = bench.get_bug(task_dir.name)
        with tempfile.TemporaryDirectory() as ws:
            task = build_task(bug, stage_workspace(bug, Path(ws)))
        ok_sys = render(harbor["system_template"], "") == render(upstream["system_template"], "")
        ok_inst = render(harbor["instance_template"], (task_dir / "instruction.md").read_text()) == render(upstream["instance_template"], task)
        failures += not (ok_sys and ok_inst)
        print(f"{task_dir.name}: system {'OK' if ok_sys else 'DIFF'}, instance {'OK' if ok_inst else 'DIFF'}")
    print(f"{failures} mismatching task(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
