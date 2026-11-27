#!/usr/bin/env python3
"""Summarise LatchBio parity runs: per-run accuracy, mean +/- sample SEM, overlap.

    python parity/summarize.py \\
        --original parity-experiments/latchbio/original_parity/run1 .../run2 .../run3 \\
        --harbor   jobs/latchbio-parity-harbor-run1 .../run2 .../run3

``--original`` takes run directories written by ``parity/run_original.py``;
``--harbor`` takes Harbor job directories. The metric is the benchmark's own:
accuracy = evaluations passed / evaluations attempted, in percent. Like
upstream's batch runner, an evaluation without a gradable answer counts as not
passed.

Both sides are classified with one rule, so that neither side's failures are
treated more leniently than the other's. A trial is a *scored outcome* when the
agent ran to the end of its turn or hit the time limit; whatever it submitted
(or failed to submit) is then graded. It is an *infrastructure failure* when
the run never got that far: the harness or environment raised, or the agent CLI
stopped abnormally (for example an API or rate-limit error, which Claude Code
reports with ``is_error`` on its final result event). Infrastructure failures
are listed for re-running instead of being silently scored.

Prints the ``metrics`` entry for ``parity_experiment.json``, the README table
row, the range-overlap verdict (adapters.mdx, Step 5), and a per-task table.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

# Harbor exceptions after which the trial is still a genuine, graded outcome:
# the agent used up its time, or its CLI exited non-zero after working (the
# stream check below still catches API errors among those).
SCORED_EXCEPTIONS = {"AgentTimeoutError", "NonZeroAgentExitCodeError"}


def mean(values: list[float]) -> float:
    return sum(values) / len(values)


def sample_sem(values: list[float]) -> float:
    """sqrt( sum((x - mean)^2) / (n * (n - 1)) ); undefined for n < 2."""
    n = len(values)
    if n < 2:
        raise ValueError("sample SEM needs at least two runs")
    m = mean(values)
    return math.sqrt(sum((x - m) ** 2 for x in values) / (n * (n - 1)))


def ranges_overlap(a: list[float], b: list[float]) -> bool:
    return max(a) >= min(b) and max(b) >= min(a)


def fmt(values: list[float]) -> str:
    if len(values) < 2:
        return f"{mean(values):.2f} ± n/a"
    return f"{mean(values):.2f} ± {sample_sem(values):.2f}"


def final_result_event(events: list[Any]) -> dict | None:
    """The last ``result`` event of a Claude Code stream-json transcript."""
    for event in reversed(events):
        if isinstance(event, dict) and event.get("type") == "result":
            return event
    return None


def agent_stream_problem(events: list[Any] | None, timed_out: bool) -> str | None:
    """Why the agent CLI did not finish its turn normally, or ``None`` if it did.

    ``events`` is ``None`` when no transcript is available, in which case only
    harness-level signals can be used.
    """
    if timed_out or events is None:
        return None
    result = final_result_event(events)
    if result is None:
        return "agent CLI produced no final result event"
    if result.get("is_error"):
        detail = str(result.get("result") or result.get("subtype") or "")[:120]
        return f"agent CLI ended with an error: {detail}"
    return None


def _read_stream_lines(path: Path) -> list[Any] | None:
    if not path.is_file():
        return None
    events = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def load_original(run_dir: Path) -> tuple[dict[str, bool], list[str]]:
    results: dict[str, bool] = {}
    errors: list[str] = []
    for path in sorted(run_dir.glob("*/result.json")):
        record = json.loads(path.read_text())
        task = record["task_id"]
        results[task] = bool(record["passed"])
        if record.get("harness_error"):
            errors.append(f"{task} (harness error: {str(record['harness_error'])[:80]})")
            continue
        timed_out = bool((record.get("metadata") or {}).get("timed_out"))
        trajectory = path.with_name("trajectory.json")
        events = json.loads(trajectory.read_text()) if trajectory.is_file() else None
        problem = agent_stream_problem(events if isinstance(events, list) else None, timed_out)
        if problem:
            errors.append(f"{task} ({problem})")
    if not results:
        raise SystemExit(f"No */result.json found in original run directory {run_dir}")
    return results, errors


def load_harbor(job_dir: Path) -> tuple[dict[str, bool], list[str]]:
    # A resumed job can hold more than one trial per task; keep the latest.
    latest: dict[str, tuple[str, Path, dict]] = {}
    for path in sorted(job_dir.glob("*/result.json")):
        record = json.loads(path.read_text())
        name = str(record.get("task_name") or path.parent.name).split("/")[-1]
        stamp = str(record.get("finished_at") or record.get("started_at") or "")
        if name not in latest or stamp >= latest[name][0]:
            latest[name] = (stamp, path, record)
    if not latest:
        raise SystemExit(f"No trial results found in Harbor job directory {job_dir}")

    results: dict[str, bool] = {}
    errors: list[str] = []
    for name, (_, path, record) in sorted(latest.items()):
        rewards = (record.get("verifier_result") or {}).get("rewards") or {}
        results[name] = float(rewards.get("reward", 0.0)) >= 1.0
        exception = (record.get("exception_info") or {}).get("exception_type")
        if exception and exception not in SCORED_EXCEPTIONS:
            errors.append(f"{name} ({exception})")
            continue
        if not rewards:
            errors.append(f"{name} (no verifier result)")
            continue
        events = _read_stream_lines(path.parent / "agent" / "claude-code.txt")
        problem = agent_stream_problem(events, timed_out=exception == "AgentTimeoutError")
        if problem:
            errors.append(f"{name} ({problem})")
    return results, errors


def accuracy(results: dict[str, bool]) -> float:
    return round(100.0 * sum(results.values()) / len(results), 2)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--original", nargs="+", type=Path, required=True)
    parser.add_argument("--harbor", nargs="+", type=Path, required=True)
    parser.add_argument("--benchmark-name", default="LatchBio Benchmarks")
    parser.add_argument(
        "--adapted-size",
        type=int,
        default=None,
        help="Size of the full adapted dataset, for the README row's percentage",
    )
    parser.add_argument("--output", type=Path, help="Write the summary as JSON")
    args = parser.parse_args()

    sides = {
        "original": [load_original(path) for path in args.original],
        "harbor": [load_harbor(path) for path in args.harbor],
    }

    tasks = sorted(set().union(*(run[0].keys() for runs in sides.values() for run in runs)))
    problems: list[str] = []
    for side, runs in sides.items():
        for index, (results, errors) in enumerate(runs, 1):
            missing = sorted(set(tasks) - set(results))
            if missing:
                problems.append(f"{side} run {index}: {len(missing)} task(s) missing, e.g. {missing[:3]}")
            if errors:
                problems.append(f"{side} run {index}: infrastructure failures: {errors}")
    if len(args.original) != len(args.harbor):
        problems.append(
            f"run counts differ: {len(args.original)} original vs {len(args.harbor)} harbor"
        )

    runs = {side: [accuracy(results) for results, _ in data] for side, data in sides.items()}
    overlap = ranges_overlap(runs["original"], runs["harbor"])

    metric = {
        "benchmark_name": args.benchmark_name,
        "metric": "accuracy (% of evaluations passed)",
        "original": fmt(runs["original"]),
        "harbor": fmt(runs["harbor"]),
        "original_runs": runs["original"],
        "harbor_runs": runs["harbor"],
    }

    per_task = {
        task: {
            side: sum(results.get(task, False) for results, _ in data)
            for side, data in sides.items()
        }
        for task in tasks
    }
    summary = {
        "n_tasks": len(tasks),
        "number_of_runs": {side: len(data) for side, data in sides.items()},
        "metric": metric,
        "ranges_overlap": overlap,
        "problems": problems,
        "per_task_passes": per_task,
    }
    if args.output:
        args.output.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")

    print("parity_experiment.json metrics entry:")
    print(json.dumps(metric, indent=2, ensure_ascii=False))
    n = len(args.original)
    adapted = args.adapted_size or len(tasks)
    share = f"{len(tasks)} ({100 * len(tasks) / adapted:.0f}%)"
    print("\nREADME row:")
    print(
        f"| <agent>@<version> | <model> | accuracy (%) | {n} | {share} | "
        f"{metric['original']} | {metric['harbor']} |"
    )
    print(
        f"\nRun-score ranges: original [{min(runs['original'])}, {max(runs['original'])}], "
        f"harbor [{min(runs['harbor'])}, {max(runs['harbor'])}] -> "
        f"{'OVERLAP (match)' if overlap else 'NO OVERLAP (investigate before reporting)'}"
    )

    n_orig, n_harbor = len(args.original), len(args.harbor)
    print(f"\nPer-task passes (original /{n_orig}, harbor /{n_harbor}); '*' marks a difference:")
    for task, counts in per_task.items():
        flag = "*" if counts["original"] * n_harbor != counts["harbor"] * n_orig else " "
        print(f" {flag} {counts['original']}/{n_orig}  {counts['harbor']}/{n_harbor}  {task}")
    only_original = [t for t, c in per_task.items() if c["original"] and not c["harbor"]]
    only_harbor = [t for t, c in per_task.items() if c["harbor"] and not c["original"]]
    print(f"\nPassed only on original: {only_original}")
    print(f"Passed only on harbor:   {only_harbor}")

    for problem in problems:
        print(f"WARNING: {problem}", file=sys.stderr)
    return 0 if overlap and not problems else 1


if __name__ == "__main__":
    sys.exit(main())
