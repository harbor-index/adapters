"""Check that the judge panel separates right reports from wrong ones.

The oracle shortcut gives reward 1 without calling the judges, so an oracle run
proves only the plumbing. This script builds the two controls that exercise the
judges, both regraded through Harbor with the shortcut off:

  positive  each task's own oracle report
  negative  the oracle report of a different task (reports rotated by one)

Usage (from the adapters repo root):

  uv run src/frontier-finance/judge_calibration.py swap jobs/<oracle-job> jobs/<oracle-job>-swapped
  harbor job regrade jobs/<oracle-job> -p datasets/frontier-finance --ve FF_ORACLE_SHORTCUT=0
  harbor job regrade jobs/<oracle-job>-swapped -p datasets/frontier-finance --ve FF_ORACLE_SHORTCUT=0
  uv run src/frontier-finance/judge_calibration.py summary jobs/<positive-regrade> jobs/<negative-regrade>
"""

from __future__ import annotations

import argparse
import json
import shutil
import statistics
from collections import defaultdict
from pathlib import Path

REPORT = Path("artifacts/app/report.md")


def trial_dirs(job: Path) -> list[Path]:
    return sorted(p for p in job.iterdir() if (p / "result.json").is_file())


def swap(src: Path, dst: Path) -> None:
    if dst.exists():
        raise SystemExit(f"{dst} already exists")
    shutil.copytree(src, dst)
    trials = trial_dirs(dst)
    if len(trials) < 2:
        raise SystemExit("need at least two trials to swap reports")
    reports = [(t / REPORT).read_bytes() for t in trials]
    for i, t in enumerate(trials):
        (t / REPORT).write_bytes(reports[(i + 1) % len(reports)])
    print(f"Rotated report.md across {len(trials)} trials into {dst}")


def summarize(job: Path) -> None:
    rewards, errors, short_panel = [], [], 0
    by_type: dict[str, list[bool]] = defaultdict(list)
    for t in trial_dirs(job):
        grading = t / "verifier" / "grading.json"
        g = json.loads(grading.read_text()) if grading.is_file() else {}
        if g.get("oracle_shortcut"):
            raise SystemExit(f"{t.name} used the oracle shortcut; regrade with FF_ORACLE_SHORTCUT=0")
        if g.get("failed", True) or "qualification_rate" not in g:
            errors.append(t.name)
            continue
        rewards.append(g["qualification_rate"])
        short_panel += g["judges_voted"] < len(g["judge_models"])
        for j in g["judgements"]:
            by_type[j["rubric_type"]].append(j["qualified"])

    print(f"== {job}")
    if rewards:
        sem = statistics.stdev(rewards) / len(rewards) ** 0.5 if len(rewards) > 1 else 0.0
        print(f"graded {len(rewards)}  mean {statistics.mean(rewards):.3f} ± {sem:.3f} (SEM)  "
              f"min {min(rewards):.3f}  max {max(rewards):.3f}")
    print(f"no reward (judge_error or missing): {len(errors)}  fewer judges than configured: {short_panel}")
    for rtype, labels in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
        print(f"  {rtype:40s} {sum(labels):5d}/{len(labels):<5d} {sum(labels) / len(labels):.3f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("swap", help="copy a job and rotate report.md across its trials")
    p.add_argument("src", type=Path)
    p.add_argument("dst", type=Path)
    p = sub.add_parser("summary", help="summarize regraded jobs")
    p.add_argument("jobs", type=Path, nargs="+")
    args = parser.parse_args()
    if args.cmd == "swap":
        swap(args.src, args.dst)
    else:
        for job in args.jobs:
            summarize(job)


if __name__ == "__main__":
    main()
