#!/usr/bin/env python3
"""Summarize SWEeper-Bench parity runs (original vs Harbor) and the oracle run.

Original side: status.json files written by scripts/run_upstream_local.py
(--mode run), one directory per run. Harbor side: Harbor job directories, one or
more per run (retries of infrastructure failures are merged; a finished reward
beats an exception). A task is solved iff both the target and the preservation
workflow pass (Harbor reward 1).

Reports per run: solved rate (%) on the parity cohort; per side: mean and sample
SEM; whether the run ranges overlap; MSE of paired per-task solve rates.

Example:
    python scripts/summarize_parity.py \
      --original runs/upstream/r1 runs/upstream/r2 runs/upstream/r3 \
      --harbor "jobs/parity-r1,jobs/parity-r1-retry" "jobs/parity-r2,jobs/parity-r2-retry" \
               "jobs/parity-r3,jobs/parity-r3-retry" \
      --task-ids-file parity_ids.json --oracle-jobs "jobs/oracle-final-*" --out parity_summary.json
"""

from __future__ import annotations

import argparse
import glob
import json
import math
from pathlib import Path


def harbor_run(dirs: list[str]) -> dict[str, dict]:
    best: dict[str, dict] = {}
    for root in dirs:
        for d in sorted(Path(root).glob("sweeper-*__*/")):
            f = d / "result.json"
            if not f.is_file():
                continue
            r = json.loads(f.read_text())
            if not r.get("finished_at"):
                continue
            task = d.name.split("__")[0]
            reward = ((r.get("verifier_result") or {}).get("rewards") or {}).get("reward")
            exc = (r.get("exception_info") or {}).get("exception_type")
            item = {"reward": reward, "exception": exc, "trial": str(d)}
            prev = best.get(task)
            # A scored result (reward present) beats an exception-only result.
            if prev is None or (prev["reward"] is None and reward is not None):
                best[task] = item
    return best


def original_run(root: str) -> dict[str, dict]:
    out = {}
    for f in sorted(Path(root).glob("*/status.json")):
        s = json.loads(f.read_text())
        out[s["case"]] = {"solved": s.get("solved"), "patch": s.get("patch"), "dir": str(f.parent)}
    return out


def mean_sem(xs: list[float]) -> tuple[float, float]:
    n = len(xs)
    m = sum(xs) / n
    sem = math.sqrt(sum((x - m) ** 2 for x in xs) / (n * (n - 1))) if n > 1 else float("nan")
    return m, sem


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--original", nargs="+", required=True)
    p.add_argument("--harbor", nargs="+", required=True, help="comma-separated job dirs per run")
    p.add_argument("--task-ids-file", required=True)
    p.add_argument("--oracle-jobs", default=None, help="glob of oracle job dirs")
    p.add_argument("--out", required=True)
    a = p.parse_args()

    ids = json.loads(Path(a.task_ids_file).read_text())
    orig = [original_run(r) for r in a.original]
    harb = [harbor_run(spec.split(",")) for spec in a.harbor]

    def score_orig(run):
        # Not run / infrastructure failure is reported separately, never counted as a pass.
        return {t: (1.0 if (run.get(t) or {}).get("solved") is True else 0.0) for t in ids}

    def score_harb(run):
        # Upstream writes no patch when the agent exits non-zero (run.sh) and the
        # case is not evaluated; count such Harbor trials as unsolved as well.
        def solved(item):
            if item.get("exception") == "NonZeroAgentExitCodeError":
                return 0.0
            return 1.0 if item.get("reward") == 1 else 0.0
        return {t: solved(run.get(t) or {}) for t in ids}

    o_scores = [score_orig(r) for r in orig]
    h_scores = [score_harb(r) for r in harb]
    o_runs = [round(100 * sum(s.values()) / len(ids), 2) for s in o_scores]
    h_runs = [round(100 * sum(s.values()) / len(ids), 2) for s in h_scores]
    o_m, o_sem = mean_sem(o_runs)
    h_m, h_sem = mean_sem(h_runs)
    overlap = max(o_runs) >= min(h_runs) and max(h_runs) >= min(o_runs)
    per_task = {
        t: {
            "original": [s[t] for s in o_scores],
            "harbor": [s[t] for s in h_scores],
        }
        for t in ids
    }
    mse = sum(
        (sum(v["original"]) / len(v["original"]) - sum(v["harbor"]) / len(v["harbor"])) ** 2
        for v in per_task.values()
    ) / len(ids)
    missing = {
        "original": [[t for t in ids if t not in r or r[t].get("solved") is None] for r in orig],
        "harbor": [[t for t in ids if t not in r or r[t].get("reward") is None] for r in harb],
    }
    summary = {
        "cohort_size": len(ids),
        "metric": "solved_rate_percent",
        "original_runs": o_runs,
        "harbor_runs": h_runs,
        "original": f"{o_m:.2f} ± {o_sem:.2f}",
        "harbor": f"{h_m:.2f} ± {h_sem:.2f}",
        "ranges_overlap": overlap,
        "paired_task_mse": round(mse, 4),
        "unscored": missing,
        "per_task": per_task,
    }
    if a.oracle_jobs:
        oracle = harbor_run(sorted(glob.glob(a.oracle_jobs)))
        summary["oracle"] = {
            "tasks": len(oracle),
            "reward_1": sorted(t for t, v in oracle.items() if v["reward"] == 1),
            "not_1": {t: v for t, v in oracle.items() if v["reward"] != 1},
        }
        summary["oracle"]["pass_count"] = len(summary["oracle"]["reward_1"])
    Path(a.out).write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k not in ("per_task",)}, indent=2)[:3000])


if __name__ == "__main__":
    main()
