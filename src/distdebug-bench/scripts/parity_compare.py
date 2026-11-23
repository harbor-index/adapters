"""Compare parity runs of the original benchmark and the Harbor adapter.

Each side is a list of results directories in the upstream layout
(``<bug>/run-01/diagnosis.json``), one directory per run: the original side as
written by ``python -m distdebug_bench.run --runs 1 --max-attempts 1 --output ...``,
the Harbor side as converted by ``harbor_job_to_distdebug.py`` (one Harbor job =
one run). Both sides are rescored here with the same upstream ``score_run``.

Protocol (identical on both sides): one attempt per bug per run; a missing or
schema-invalid diagnosis scores 0 on every metric. A run's score is the mean
over the parity bugs. We report mean ± sample SEM over runs, the range-overlap
matching criterion from the Harbor adapter guide, and the per-bug mean squared
difference of the run-averaged scores (MSE) as a supplementary statistic.

Usage:
    python parity_compare.py --upstream /path/to/distdebug-bench --bugs bugs.txt \
        --original runs/orig-1 runs/orig-2 runs/orig-3 \
        --harbor   runs/harbor-1 runs/harbor-2 runs/harbor-3 [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from statistics import mean

METRICS = ("block_coverage", "file_coverage", "function_coverage", "line_coverage", "strict_block_coverage", "block_precision", "mention_coverage")


def sem(xs: list[float]) -> float:
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) * (len(xs) - 1))) if len(xs) > 1 else float("nan")


def score_side(bench, score_run, bugs: list[str], run_dirs: list[Path]) -> dict:
    per_run, per_bug, invalid = [], {b: [] for b in bugs}, []
    for run_dir in run_dirs:
        scores = {}
        n_invalid = 0
        for bug_id in bugs:
            res = score_run(bench.get_bug(bug_id), run_dir / bug_id / "run-01", write=False) if (run_dir / bug_id / "run-01").is_dir() else None
            if res is None:
                n_invalid += 1
                res = dict.fromkeys(METRICS, 0.0)
            scores[bug_id] = {m: float(res[m]) for m in METRICS}
            per_bug[bug_id].append(scores[bug_id])
        per_run.append({m: mean(s[m] for s in scores.values()) for m in METRICS})
        invalid.append(n_invalid)
    return {"per_run": per_run, "per_bug": per_bug, "invalid_per_run": invalid}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--upstream", type=Path, required=True, help="Checkout of OrderLab/distdebug-bench (pinned commit)")
    parser.add_argument("--bugs", type=Path, required=True, help="File with one bug id per line (the parity subset)")
    parser.add_argument("--original", type=Path, nargs="+", required=True)
    parser.add_argument("--harbor", type=Path, nargs="+", required=True)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()

    sys.path.insert(0, str(args.upstream.resolve()))
    from distdebug_bench import load_benchmark
    from distdebug_bench.score import score_run

    bench = load_benchmark(args.upstream)
    bugs = args.bugs.read_text().split()
    sides = {"original": score_side(bench, score_run, bugs, args.original), "harbor": score_side(bench, score_run, bugs, args.harbor)}

    report = {"bugs": bugs, "n_bugs": len(bugs), "metrics": {}}
    print(f"{len(bugs)} bugs; runs: original {len(args.original)}, harbor {len(args.harbor)}")
    print(f"invalid/missing diagnoses per run: original {sides['original']['invalid_per_run']}, harbor {sides['harbor']['invalid_per_run']}")
    for m in METRICS:
        o = [100 * r[m] for r in sides["original"]["per_run"]]
        h = [100 * r[m] for r in sides["harbor"]["per_run"]]
        overlap = max(o) >= min(h) and max(h) >= min(o)
        mse = mean(
            (mean(x[m] for x in sides["original"]["per_bug"][b]) - mean(x[m] for x in sides["harbor"]["per_bug"][b])) ** 2
            for b in bugs
        )
        report["metrics"][m] = {
            "original": f"{mean(o):.2f} ± {sem(o):.2f}", "harbor": f"{mean(h):.2f} ± {sem(h):.2f}",
            "original_runs": [round(x, 2) for x in o], "harbor_runs": [round(x, 2) for x in h],
            "ranges_overlap": overlap, "per_bug_mse": round(mse, 5),
        }
        print(f"  {m:22s} original {mean(o):6.2f} ± {sem(o):5.2f} {o}  harbor {mean(h):6.2f} ± {sem(h):5.2f} {h}  "
              f"overlap={overlap}  per-bug MSE={mse:.4f}")
    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
