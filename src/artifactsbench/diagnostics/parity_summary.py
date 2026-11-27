"""Summarize ArtifactsBench parity runs on the 0-100 overall score.

Each --harbor-run is a Harbor job directory; scores come from the verifier's
judge.json files. Each --original-run is a JSONL written by original_score.py.
All runs are compared on the task IDs that every run scored. Missing IDs are
listed, never silently dropped.

Reports the official matching criterion (mean +/- sample SEM over runs, run-range
overlap), the per-task mean squared error between sides that maintainers use to
gauge closeness and, because both sides score the same tasks, a paired per-task
difference. Supply --manifest from artifactsbench.reference for a replay run to
also compare against Tencent's published scores.

With --calibrate, only --harbor-run is needed. Use it to screen candidate judges
on the replay set: it reports how many tasks produced a score, test-retest
reliability across runs (ICC(1,1)), and the shift and Spearman correlation
against Tencent's published gemini-2.5-pro scores.
"""

import argparse
import json
import math
import statistics
import sys
from pathlib import Path


def harbor_scores(job_dir):
    scores = {}
    for path in sorted(Path(job_dir).rglob("judge.json")):
        result = json.loads(path.read_text(encoding="utf-8"))
        index = int(result["index"])
        if index in scores:
            raise ValueError(f"{job_dir}: task {index} scored twice")
        scores[index] = float(result["gemini_ans"])
    if not scores:
        raise ValueError(f"{job_dir}: no verifier judge.json found")
    return scores


def original_scores(path):
    scores = {}
    with Path(path).open(encoding="utf-8") as rows:
        for row in map(json.loads, rows):
            index = int(row["index"])
            if index in scores:
                raise ValueError(f"{path}: task {index} scored twice")
            scores[index] = float(row["gemini_ans"])
    if not scores:
        raise ValueError(f"{path}: no scored rows")
    return scores


def mean_sem(values):
    mean = statistics.fmean(values)
    if len(values) < 2:
        return mean, None
    return mean, statistics.stdev(values) / math.sqrt(len(values))


def t_quantile_975(df):
    # Cornish-Fisher expansion of Student's t around the normal quantile.
    # Error is below 0.003 for df >= 10; parity uses df = tasks - 1.
    z = 1.959963984540054
    return z + (z**3 + z) / (4 * df) + (5 * z**5 + 16 * z**3 + 3 * z) / (96 * df**2)


def pearson(xs, ys):
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    return sxy / math.sqrt(sxx * syy) if sxx and syy else None


def ranks(values):
    order = sorted(range(len(values)), key=lambda i: values[i])
    result = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start
        while end + 1 < len(order) and values[order[end + 1]] == values[order[start]]:
            end += 1
        for position in range(start, end + 1):
            result[order[position]] = (start + end) / 2 + 1
        start = end + 1
    return result


def spearman(xs, ys):
    return pearson(ranks(xs), ranks(ys))


def icc_1_1(table):
    # One-way random effects, single rating: rows are tasks, columns are runs.
    n, k = len(table), len(table[0])
    grand = statistics.fmean(v for row in table for v in row)
    row_means = [statistics.fmean(row) for row in table]
    msb = k * sum((m - grand) ** 2 for m in row_means) / (n - 1)
    msw = sum((v - m) ** 2 for row, m in zip(table, row_means) for v in row) / (n * (k - 1))
    return (msb - msw) / (msb + (k - 1) * msw) if msb + (k - 1) * msw else None


def calibrate(runs, expected, published, models):
    universe = set(expected)
    common = set(universe)
    for run in runs:
        common &= run.keys()
    ids = sorted(common)
    if len(ids) < 3:
        raise ValueError("Too few tasks were scored by every run")
    table = [[run[i] for run in runs] for i in ids]
    means = [statistics.fmean(row) for row in table]
    base = [published[i] for i in ids]
    shift, shift_se = mean_sem([m - b for m, b in zip(means, base)])
    report = {
        "returned_models": sorted(models),
        "tasks_expected": len(universe),
        "tasks_scored_by_run": [len(universe & run.keys()) for run in runs],
        "tasks_compared": len(ids),
        "missing_by_run": [sorted(universe - run.keys()) for run in runs],
        "run_means": [round(statistics.fmean(run[i] for i in ids), 4) for run in runs],
        "mean_shift_vs_published": round(shift, 4),
        "spearman_vs_published": round(spearman(means, base), 4),
        "pearson_vs_published": round(pearson(means, base), 4),
    }
    if shift_se is not None:
        report["mean_shift_se"] = round(shift_se, 4)
    if len(runs) > 1:
        report["icc_1_1"] = round(icc_1_1(table), 4)
        pairs = [(a, b) for a in range(len(runs)) for b in range(a + 1, len(runs))]
        report["test_retest_spearman"] = round(statistics.fmean(
            spearman([row[a] for row in table], [row[b] for row in table]) for a, b in pairs), 4)
    return report


def summarize(original_runs, harbor_runs, expected=None, published=None):
    runs = {"original": original_runs, "harbor": harbor_runs}
    universe = set(expected) if expected else set().union(*(r for side in runs.values() for r in side))
    common = set(universe)
    for side in runs.values():
        for run in side:
            common &= run.keys()
    missing = {
        name: [sorted(universe - run.keys()) for run in side] for name, side in runs.items()
    }
    if not common:
        raise ValueError("No task was scored by every run")
    ids = sorted(common)

    report = {"tasks_compared": len(ids), "tasks_expected": len(universe), "missing_by_run": missing}
    per_task = {}
    for name, side in runs.items():
        run_means = [statistics.fmean(run[i] for i in ids) for run in side]
        mean, sem = mean_sem(run_means)
        per_task[name] = {i: statistics.fmean(run[i] for run in side) for i in ids}
        within = [statistics.stdev([run[i] for run in side]) for i in ids] if len(side) > 1 else None
        report[name] = {
            "runs": [round(x, 4) for x in run_means],
            "mean": round(mean, 4),
            "sem": None if sem is None else round(sem, 4),
            "formatted": f"{mean:.2f} ± {sem:.2f}" if sem is not None else f"{mean:.2f} (single run)",
            "mean_within_task_sd": None if within is None else round(statistics.fmean(within), 4),
        }
    o, h = report["original"]["runs"], report["harbor"]["runs"]
    report["ranges_overlap"] = max(o) >= min(h) and max(h) >= min(o)

    diffs = [per_task["harbor"][i] - per_task["original"][i] for i in ids]
    d_mean, d_se = mean_sem(diffs)
    paired = {"mean_difference": round(d_mean, 4), "mean_abs_difference": round(statistics.fmean(map(abs, diffs)), 4),
              "tasks_abs_difference_over_10": sum(abs(d) > 10 for d in diffs)}
    if d_se is not None:
        half = t_quantile_975(len(diffs) - 1) * d_se
        paired.update(se=round(d_se, 4), ci95=[round(d_mean - half, 4), round(d_mean + half, 4)])
    paired["largest"] = sorted(({"index": i, "difference": round(d, 2)} for i, d in zip(ids, diffs)),
                               key=lambda x: -abs(x["difference"]))[:5]
    report["paired_harbor_minus_original"] = paired
    report["per_task_mse"] = round(statistics.fmean(d * d for d in diffs), 4)

    if published:
        shared = [i for i in ids if i in published]
        base = [published[i] for i in shared]
        report["published"] = {
            "tasks": len(shared),
            "mean": round(statistics.fmean(base), 4),
            **{f"pearson_{name}": round(pearson(base, [per_task[name][i] for i in shared]), 4)
               for name in runs},
        }
    # Paste into the `metrics` array of parity_experiment.json.
    report["parity_experiment_metric"] = {
        "benchmark_name": "ArtifactsBench",
        "metric": "Mean overall score (0-100)",
        "original": report["original"]["formatted"],
        "harbor": report["harbor"]["formatted"],
        "original_runs": report["original"]["runs"],
        "harbor_runs": report["harbor"]["runs"],
    }
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--original-run", action="append", default=[], type=Path)
    parser.add_argument("--harbor-run", action="append", required=True, type=Path)
    parser.add_argument("--manifest", type=Path, help="manifest.json from artifactsbench.reference")
    parser.add_argument("--task-ids", type=int, nargs="+", help="Expected IDs when no manifest is supplied")
    parser.add_argument("--calibrate", action="store_true", help="Screen a judge on Harbor replay runs only")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.calibrate:
        if not args.manifest or args.original_run:
            parser.exit(1, "parity_summary: --calibrate needs --manifest and only --harbor-run\n")
    elif len(args.original_run) != len(args.harbor_run):
        parser.exit(1, "parity_summary: both sides need the same number of runs\n")

    expected, published = args.task_ids, None
    if args.manifest:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        published = {e["index"]: e["published_score"] for e in manifest["imported"]}
        expected = expected or sorted(published)
    if args.calibrate:
        models = {json.loads(p.read_text(encoding="utf-8")).get("returned_model")
                  for run in args.harbor_run for p in Path(run).rglob("judge.json")}
        report = calibrate([harbor_scores(p) for p in args.harbor_run], expected, published, models)
    else:
        report = summarize([original_scores(p) for p in args.original_run],
                           [harbor_scores(p) for p in args.harbor_run], expected, published)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    missing = report["missing_by_run"]
    if any(any(m) for m in (missing.values() if isinstance(missing, dict) else [missing])):
        print("WARNING: some runs are missing tasks; resolve or disclose before reporting.", file=sys.stderr)


if __name__ == "__main__":
    main()
