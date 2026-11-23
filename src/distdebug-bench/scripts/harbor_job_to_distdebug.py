"""Convert a Harbor job directory into the upstream DistDebug-Bench results layout.

    <out>/<bug>/run-NN/diagnosis.json        (only if the agent left one)
    <out>/<bug>/run-NN/trajectory.json       (Mini-SWE-Agent native trace, if present)
    <out>/<bug>/run-NN/session_stats.json    (tokens / cost from Harbor's result.json)
    <out>/<bug>/run-NN/harbor_trial.txt      (source trial directory)

The upstream tools can then be run unchanged on Harbor results:

    python -m distdebug_bench.score   <out> --subset lite --attempts 1
    python -m distdebug_bench.leakage <out>

Trials of the same bug are numbered run-01, run-02, ... in start-time order.
Directories whose name starts with "_" (e.g. ``_infra_failures``, ``_excluded``) are skipped.

``--redact-env NAME`` (repeatable) replaces the value of environment variable NAME with
``<REDACTED>`` in the copied files, including copies that the terminal line-wrapped.
Harbor's mini-swe-agent log echoes the API key, and an agent that reads
``/logs/agent/mini-swe-agent.txt`` copies it into its trajectory.

Usage: python harbor_job_to_distdebug.py <harbor-job-dir | dir-of-jobs> <out-dir> [--redact-env NAME ...]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


def bug_of(task_name: str) -> str:
    # "orderlab/distdebug-bench__zookeeper-2845" -> "zookeeper-2845"
    return task_name.rsplit("/", 1)[-1].split("__", 1)[-1]


def secret_patterns(names: list[str]) -> list[re.Pattern[str]]:
    patterns = []
    for name in names:
        value = os.environ.get(name)
        if not value:
            sys.exit(f"--redact-env {name}: variable is not set")
        # Allow a (possibly JSON-escaped) line break between any two characters.
        patterns.append(re.compile(r"(?:\\\\n|\\n|\n)?".join(map(re.escape, value))))
    return patterns


def copy_redacted(src: Path, dst: Path, patterns: list[re.Pattern[str]]) -> None:
    text = src.read_text(errors="surrogateescape")
    for pattern in patterns:
        text = pattern.sub("<REDACTED>", text)
    dst.write_text(text, errors="surrogateescape")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("job", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--redact-env", action="append", default=[])
    args = ap.parse_args()
    job, out, patterns = args.job, args.out, secret_patterns(args.redact_env)
    trials = []
    # Works for one job (<job>/<trial>/result.json) or a directory of per-task jobs.
    for result_path in sorted(job.rglob("result.json")):
        if any(part.startswith("_") for part in result_path.relative_to(job).parts):
            continue
        result = json.loads(result_path.read_text())
        if "task_name" not in result:
            continue  # job-level result.json
        trials.append((result.get("started_at") or "", result_path.parent, result))
    trials.sort(key=lambda t: (bug_of(t[2]["task_name"]), t[0]))
    counts: dict[str, int] = {}
    for _, trial_dir, result in trials:
        bug = bug_of(result["task_name"])
        counts[bug] = counts.get(bug, 0) + 1
        run_dir = out / bug / f"run-{counts[bug]:02d}"
        run_dir.mkdir(parents=True, exist_ok=True)
        diagnosis = trial_dir / "artifacts" / "workspace" / "diagnosis.json"
        if diagnosis.is_file():
            copy_redacted(diagnosis, run_dir / "diagnosis.json", patterns)
        trace = trial_dir / "agent" / "mini-swe-agent.trajectory.json"
        if trace.is_file():
            copy_redacted(trace, run_dir / "trajectory.json", patterns)
        agent = result.get("agent_result") or {}
        stats = {
            "input_tokens": agent.get("n_input_tokens"),
            "input_cached_tokens": agent.get("n_cache_tokens"),
            "output_tokens": agent.get("n_output_tokens"),
            "total_tokens": (agent.get("n_input_tokens") or 0) + (agent.get("n_output_tokens") or 0),
            "cost": agent.get("cost_usd"),
            "harbor_exception": (result.get("exception_info") or {}).get("exception_type"),
            "harbor_rewards": (result.get("verifier_result") or {}).get("rewards"),
        }
        (run_dir / "session_stats.json").write_text(json.dumps(stats, indent=2) + "\n")
        (run_dir / "harbor_trial.txt").write_text(str(trial_dir.resolve()) + "\n")
    print(f"Converted {len(trials)} trials of {len(counts)} bugs into {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
