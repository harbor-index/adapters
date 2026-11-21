"""Collect parity scores from both sides and check the overlap criterion.

Original side: OUT_DIR/run_<N>/<task_id>/evaluation_result_<level>_1.md written
by upstream (``run_original.sh``). Harbor side: one Harbor job directory per
run; each trial's ``verifier/score.json`` written by ``tests/score.py``.

    python parity/collect_parity.py --original parity_runs/original \
        --harbor jobs/parity-run1 jobs/parity-run2 jobs/parity-run3 \
        [--cases parity/parity_cases.txt] [--json out.json]

Metrics per run (both from upstream's evaluator): strictly correct returns (%)
and mean correct-by-line (%), over the cases that produced a scored return.
Missing returns and errored trials are reported, not scored as 0.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path


def _cases(path: Path) -> list[str]:
    return [l.split()[0] for l in path.read_text().splitlines() if l.strip() and not l.startswith("#")]


def _original_run(run_dir: Path, cases: list[str], level: str) -> dict[str, dict]:
    out = {}
    for task_id in cases:
        f = run_dir / task_id / f"evaluation_result_{level}_1.md"
        if not f.is_file():
            continue
        text = f.read_text()
        strict = re.search(r"Strictly correct return: (True|False)", text)
        by_line = re.search(r"Correct \(by line\): ([\d.]+)%", text)
        if strict and by_line:
            out[task_id] = {"strict": strict.group(1) == "True", "by_line": float(by_line.group(1))}
    return out


def _harbor_run(job_dir: Path, cases: list[str]) -> dict[str, dict]:
    out = {}
    for trial in sorted(p for p in job_dir.iterdir() if p.is_dir()):
        result_file = trial / "result.json"
        if not result_file.is_file():
            continue
        result = json.loads(result_file.read_text())
        # Trial folder names are truncated; result.json has the full task name
        # ("column-tax/taxcalcbench__<task_id>").
        task_id = result["task_name"].rsplit("__", 1)[-1]
        score = trial / "verifier" / "score.json"
        if task_id not in cases or not score.is_file() or result.get("exception_info"):
            continue
        if task_id in out:
            raise SystemExit(f"{job_dir}: more than one scored trial for {task_id}; one trial per task per run")
        s = json.loads(score.read_text())
        out[task_id] = {"strict": bool(s["strictly_correct_return"]), "by_line": 100 * s["correct_by_line_score"]}
    return out


def _summ(runs: list[dict[str, dict]]) -> dict:
    if not runs:
        raise SystemExit("no runs found")
    strict = [100 * sum(r["strict"] for r in run.values()) / len(run) if run else float("nan") for run in runs]
    by_line = [sum(r["by_line"] for r in run.values()) / len(run) if run else float("nan") for run in runs]
    return {"strict_runs": strict, "by_line_runs": by_line, "n_scored": [len(r) for r in runs],
            "strict": _mean_sem(strict), "by_line": _mean_sem(by_line)}


def _mean_sem(xs: list[float]) -> tuple[float, float | None]:
    n = len(xs)
    m = sum(xs) / n
    if n < 2:
        return m, None
    return m, math.sqrt(sum((x - m) ** 2 for x in xs) / (n * (n - 1)))


def _overlap(a: list[float], b: list[float]) -> bool:
    return max(a) >= min(b) and max(b) >= min(a)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--original", type=Path, required=True)
    p.add_argument("--harbor", type=Path, nargs="+", required=True)
    p.add_argument("--cases", type=Path, default=Path(__file__).parent / "parity_cases.txt")
    p.add_argument("--level", default="high")
    p.add_argument("--json", type=Path)
    args = p.parse_args()

    cases = _cases(args.cases)
    orig_runs = [_original_run(d, cases, args.level) for d in sorted(args.original.glob("run_*")) if d.is_dir()]
    harb_runs = [_harbor_run(d, cases) for d in args.harbor]
    o, h = _summ(orig_runs), _summ(harb_runs)

    for name, s in (("original", o), ("harbor", h)):
        sm, se = s["strict"]
        bm, be = s["by_line"]
        fmt = lambda m, e: f"{m:.2f}" + (f" ± {e:.2f}" if e is not None else "")
        print(f"{name:9s} strict {fmt(sm, se)}  by-line {fmt(bm, be)}  "
              f"runs strict={[round(x, 2) for x in s['strict_runs']]} by-line={[round(x, 2) for x in s['by_line_runs']]} "
              f"scored={s['n_scored']}/{len(cases)}")
    for name, runs in (("original", orig_runs), ("harbor", harb_runs)):
        for i, run in enumerate(runs, 1):
            missing = [t for t in cases if t not in run]
            if missing:
                print(f"  {name} run {i}: not scored ({len(missing)}): {', '.join(missing)}")
    problems = []
    if len(orig_runs) < 2 or len(harb_runs) < 2:
        problems.append("fewer than 2 runs on a side")
    if len(orig_runs) != len(harb_runs):
        problems.append(f"unequal run counts ({len(orig_runs)} vs {len(harb_runs)})")
    if any(len(r) != len(cases) for r in orig_runs + harb_runs):
        problems.append("some runs did not score every case")
    print("overlap strict:", _overlap(o["strict_runs"], h["strict_runs"]),
          " overlap by-line:", _overlap(o["by_line_runs"], h["by_line_runs"]))
    if problems:
        print("NOT an acceptance report:", "; ".join(problems))

    per_task = {t: {"original": [r[t]["strict"] if t in r else None for r in orig_runs],
                    "harbor": [r[t]["strict"] if t in r else None for r in harb_runs]} for t in cases}
    if args.json:
        args.json.write_text(json.dumps({"original": o, "harbor": h, "per_task_strict": per_task}, indent=2))


if __name__ == "__main__":
    main()
