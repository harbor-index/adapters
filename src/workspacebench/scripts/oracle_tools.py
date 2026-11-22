"""Oracle outputs for Workspace-Bench: prepare task bundles, build, grade, accept.

Outputs are written by Claude Code subagents (instructions:
oracle_prompts/builder.md) as a build script per task, run in a local venv or
inside an image with LibreOffice and the office Python libs (--docker). Grading runs upstream's agent_as_a_judge.py on the plan,
the same way as Phase 1. An output set is accepted once it passes every rubric
in --times gradings in a row. Accepted outputs go to accepted/<name>/, which is
the adapter's --oracle-dir. Outputs reveal the rubric, so they ship with the
published dataset, not in this repo.

This is the tool the shipped oracle was built with, kept for reference. It
expects this layout next to its own folder (ROOT = the folder above it):
  repo/evaluation/        upstream Workspace-Bench checkout (agent_as_a_judge.py)
  full_{en,cn}_root/tasks upstream's downloaded tasks (metadata.json per task)
  phase1/.venv            Python with the office libs and the judge's deps
  phase1/judge.yaml       the judge config (claude-sonnet-5-5)
  phase1/claude_config    Claude Code config dir for the judge
The judge authenticates with a Claude subscription token read from
$WSB_TOKEN_FILE (or --token-file).

  prepare <names>   work/<name>/{task.md,rubrics.md,outputs.txt,inputs -> data/}
  build <names>     run work/<name>/build.py (local venv, or --docker); writes work/<name>/output/
  grade <names>     grade work/<name>/output/; write logs/<name>-<n>.json; print failures
                    accepted after --times clean gradings -> accepted/<name>/

Names look like en-033 / cn-033.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
EVAL = ROOT / "repo" / "evaluation"
TASKS = {"en": ROOT / "full_en_root" / "tasks", "cn": ROOT / "full_cn_root" / "tasks"}
P1 = ROOT / "phase1"
TOKEN_FILE = Path(os.environ.get("WSB_TOKEN_FILE", Path.home() / ".config/workspacebench/claude_oauth_token"))
IMAGE = "wsb-agent:dev"


def task_dir(name: str) -> Path:
    lang, num = name.split("-")
    return TASKS[lang] / str(int(num))


def prepare(names):
    for n in names:
        src = task_dir(n)
        m = json.loads((src / "metadata.json").read_text())
        d = HERE / "work" / n
        d.mkdir(parents=True, exist_ok=True)
        (d / "task.md").write_text(m["task"].strip() + "\n")
        (d / "rubrics.md").write_text("".join(f"{i}. [{t}] {r}\n" for i, (r, t) in enumerate(zip(m["rubrics"], m["rubric_types"]))))
        (d / "outputs.txt").write_text("\n".join(m["output_files"]) + "\n")
        (d / "meta.json").write_text(json.dumps({"persona": m["persona"], "language": m["language"], "id": m["id"]}, ensure_ascii=False))
        inputs = d / "inputs"
        if not inputs.exists():
            inputs.mkdir()
            for e in m["data_manifest"]:
                dst = inputs / e["filename"]
                if not dst.exists():
                    dst.symlink_to(src / e["stored_relpath"])
        print(f"{n}: {len(m['rubrics'])} rubrics, outputs {m['output_files']}, {len(m['data_manifest'])} inputs")


def build(names, docker=False):
    for n in names:
        d = HERE / "work" / n
        if not (d / "build.py").exists():
            print(f"{n}: no build.py"); continue
        out = d / "output"
        shutil.rmtree(out, ignore_errors=True); out.mkdir()
        # Inputs are symlinks into the task repo; mount the real files read-only.
        stage = d / ".inputs_real"
        shutil.rmtree(stage, ignore_errors=True); stage.mkdir()
        for p in (d / "inputs").iterdir():
            shutil.copy2(p.resolve(), stage / p.name)
        if docker:
            cmd = ["docker", "run", "--rm", "--network", "none", "-v", f"{stage}:/inputs:ro", "-v", f"{d / 'build.py'}:/build.py:ro",
                   "-v", f"{out}:/out", "-w", "/out", IMAGE, "python3", "/build.py", "/inputs", "/out"]
        else:
            # Local: the Phase 1 venv has the office libs; macOS textutil makes real .doc files.
            cmd = [str(P1 / ".venv/bin/python"), "-I", str(d / "build.py"), str(stage), str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=out)
        shutil.rmtree(stage, ignore_errors=True)
        want = [Path(x).name for x in (d / "outputs.txt").read_text().splitlines() if x.strip()]
        have = sorted(p.name for p in out.iterdir())
        missing = [w for w in want if w not in have]
        print(f"{n}: exit {r.returncode}; outputs {have}; missing {missing}")
        if r.returncode:
            print(r.stderr[-3000:])


def _grade_once(n: str) -> dict:
    d = HERE / "work" / n
    case = HERE / "cases" / n
    shutil.rmtree(case, ignore_errors=True)
    case.mkdir(parents=True)
    meta_path = task_dir(n) / "metadata.json"
    m = json.loads(meta_path.read_text()); m["__metadata_path"] = str(meta_path)
    (case / "metadata.json").write_text(json.dumps(m, ensure_ascii=False, indent=2))
    shutil.copytree(d / "output", case / "output")
    env = {"HOME": os.environ["HOME"], "USER": os.environ.get("USER", ""), "LANG": "en_US.UTF-8",
           "PATH": f"{P1}/.venv/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
           "WORKSPACE_BENCH_CLAUDE_CONFIG_DIR": str(P1 / "claude_config"),
           "CLAUDE_CODE_OAUTH_TOKEN": TOKEN_FILE.read_text().strip()}
    subprocess.run([str(P1 / ".venv/bin/python"), "-u", str(EVAL / "src/agent_as_a_judge.py"), "--task-dir", str(case),
                    "--eval-yaml", str(P1 / "judge.yaml"), "--max-retries", "1", "--overwrite"],
                   env=env, capture_output=True, text=True)
    found = glob.glob(str(case / "rubrics_judge--*.json"))
    return json.loads(Path(found[0]).read_text()) if found else {"summary": {}, "judge": {"error": "no result"}, "rubrics": []}


def grade(names, times, accept=True):
    if not TOKEN_FILE.exists():
        sys.exit("missing token file")
    for n in names:
        logs = HERE / "logs"; logs.mkdir(exist_ok=True)
        clean = 0
        for i in range(times):
            r = _grade_once(n)
            k = len(list(logs.glob(f"{n}-*.json")))
            (logs / f"{n}-{k}.json").write_text(json.dumps(r, ensure_ascii=False, indent=1))
            s = r.get("summary") or {}
            err = (r.get("judge") or {}).get("error")
            print(f"{n} grading {i + 1}: {s.get('passed')}/{s.get('total')} error={err}")
            for x in r.get("rubrics") or []:
                if not x.get("passed"):
                    print(f"   FAIL [{x.get('index')}] {str(x.get('rubric'))[:160]}\n        judge: {str(x.get('evidence') or x.get('reason'))[:400]}")
            if err or not s.get("total") or s.get("passed") != s.get("total"):
                break
            clean += 1
        if clean == times and not accept:
            print(f"{n}: PASSED {times} clean gradings (--no-accept: accepted/ not touched)")
        if clean == times and accept:
            dst = HERE / "accepted" / n
            shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(HERE / "work" / n / "output", dst)
            print(f"{n}: ACCEPTED ({times} clean gradings)")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["prepare", "build", "grade"])
    p.add_argument("names", nargs="+")
    p.add_argument("--times", type=int, default=2)
    p.add_argument("--docker", action="store_true", help="build inside the IMAGE container instead of the local venv")
    p.add_argument("--no-accept", action="store_true", help="grade only; never copy into accepted/")
    p.add_argument("--token-file", help="judge OAuth token file (default $WSB_TOKEN_FILE or ~/.config/workspacebench/claude_oauth_token)")
    a = p.parse_args()
    if a.token_file:
        global TOKEN_FILE
        TOKEN_FILE = Path(a.token_file).expanduser()
    {"prepare": lambda: prepare(a.names), "build": lambda: build(a.names, a.docker), "grade": lambda: grade(a.names, a.times, not a.no_accept)}[a.cmd]()


if __name__ == "__main__":
    main()
