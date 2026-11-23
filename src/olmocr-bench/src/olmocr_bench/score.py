"""Recompute the upstream olmOCR-Bench scores from a Harbor job directory.

Harbor's default dataset metric is the mean of the per-task rewards, i.e. the
mean over PDFs of "fraction of this PDF's tests that passed". The upstream
harness (``olmocr/bench/benchmark.py``) reports something different:

* every test is attributed to the JSONL file it comes from; the automatically
  added per-PDF baseline tests form an extra group called ``baseline``;
* each group gets a pass rate = passed tests / tests in the group;
* the headline "Average Score" (the "Overall" column of the paper) is the
  unweighted mean of those group pass rates;
* the ``±`` value is half the width of a 95% bootstrap confidence interval that
  resamples tests within each group (``olmocr/bench/utils.py``).

This script rebuilds those numbers from the per-test records that each task's
verifier writes to ``verifier/olmocr_bench_results.json``.

    uv run olmocr-bench-score jobs/<job-name>

A trial without such records (the environment did not start, the verifier
crashed or timed out, Harbor could not hand over the output file) has no score.
Leaving it out would make a job look better than it is, so, like the original
harness when a candidate has errors, the script then reports FAILED instead of a
score and exits with status 1. ``--allow-unscored`` prints the partial numbers.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

RESULTS_FILENAME = "olmocr_bench_results.json"


@dataclass
class UnitTestOutcome:
    """One upstream unit test, possibly observed in several trials of the same task."""

    test_type: str
    source: str
    passes: int = 0
    repeats: int = 0

    @property
    def pass_ratio(self) -> float:
        return self.passes / self.repeats

    @property
    def passed(self) -> bool:
        # benchmark.py: a test counts as passed if the majority of repeats pass.
        return self.pass_ratio > 0.5


@dataclass
class GroupScores:
    """Everything recorded for one (agent, model) pair in the job."""

    tests: dict[tuple[str, str], UnitTestOutcome] = field(default_factory=dict)
    rewards: list[float] = field(default_factory=list)
    unscored_trials: list[str] = field(default_factory=list)
    # Outputs that were scored as an empty document, by reason.
    empty_outputs: dict[str, int] = field(default_factory=dict)
    test_errors: int = 0
    trials_per_pdf: dict[str, int] = field(default_factory=dict)
    # Scored trials that Harbor nevertheless recorded with an exception, by type.
    scored_with_exception: dict[str, int] = field(default_factory=dict)

    def by_source(self) -> dict[str, list[UnitTestOutcome]]:
        grouped: dict[str, list[UnitTestOutcome]] = defaultdict(list)
        for outcome in self.tests.values():
            grouped[outcome.source].append(outcome)
        return dict(sorted(grouped.items()))

    def by_type(self) -> dict[str, list[UnitTestOutcome]]:
        grouped: dict[str, list[UnitTestOutcome]] = defaultdict(list)
        for outcome in self.tests.values():
            grouped[outcome.test_type].append(outcome)
        return dict(sorted(grouped.items()))


def load_job(job_dir: Path) -> dict[str, GroupScores]:
    """Read every trial of a Harbor job, grouped by "agent (model)"."""
    groups: dict[str, GroupScores] = defaultdict(GroupScores)
    trial_results = sorted(job_dir.glob("*/result.json"))
    if not trial_results:
        raise FileNotFoundError(f"No trial directories found in {job_dir}")

    for result_path in trial_results:
        trial_dir = result_path.parent
        trial = json.loads(result_path.read_text(encoding="utf-8"))
        if "trial_name" not in trial:
            raise ValueError(
                f"{result_path} is not a trial result; pass one job directory "
                "(jobs/<job-name>), not the directory that holds several jobs"
            )
        group = groups[_group_label(trial)]

        exception = (trial.get("exception_info") or {}).get("exception_type")
        results_path = trial_dir / "verifier" / RESULTS_FILENAME
        if not results_path.is_file():
            # The verifier did not run to completion (build failure, crash,
            # timeout). There is no score for this trial; report it separately.
            reason = exception or "no exception recorded"
            group.unscored_trials.append(f"{trial_dir.name} ({reason})")
            continue
        if exception:
            group.scored_with_exception[exception] = (
                group.scored_with_exception.get(exception, 0) + 1
            )

        results = json.loads(results_path.read_text(encoding="utf-8"))
        group.rewards.append(results["reward"])
        pdf = results["pdf"]
        group.trials_per_pdf[pdf] = group.trials_per_pdf.get(pdf, 0) + 1
        # Records written before output_status existed only say whether a file was found.
        status = results.get("output_status") or (
            "ok" if results.get("output_found", True) else "missing"
        )
        if status != "ok":
            group.empty_outputs[status] = group.empty_outputs.get(status, 0) + 1
        for record in results["tests"]:
            group.test_errors += bool(record.get("error"))
            key = (results["pdf"], record["id"])
            outcome = group.tests.setdefault(
                key, UnitTestOutcome(test_type=record["type"], source=record["source"])
            )
            outcome.repeats += 1
            outcome.passes += int(record["passed"])
    return dict(sorted(groups.items()))


def _group_label(trial: dict) -> str:
    agent = trial.get("agent_info") or {}
    label = agent.get("name") or "unknown-agent"
    model = agent.get("model_info") or {}
    if model.get("name"):
        label += f" ({model['name']})"
    return label


def bootstrap_ci(
    scores_by_source: list[list[float]],
    n_bootstrap: int,
    ci_level: float,
    seed: int,
) -> tuple[float, float]:
    """Port of ``calculate_bootstrap_ci(..., splits=...)`` from olmocr/bench/utils.py.

    Tests are resampled with replacement within each source; the statistic is
    the mean of the per-source means. Upstream draws from NumPy's unseeded
    global generator, so its interval differs slightly between invocations; a
    seeded generator is used here to make the output reproducible.
    """
    rng = random.Random(seed)
    means = []
    for _ in range(n_bootstrap):
        source_means = [
            sum(rng.choices(scores, k=len(scores))) / len(scores)
            for scores in scores_by_source
            if scores
        ]
        if source_means:
            means.append(sum(source_means) / len(source_means))
    if not means:
        return (0.0, 0.0)
    means.sort()
    alpha = (1 - ci_level) / 2
    return (_percentile(means, alpha * 100), _percentile(means, (1 - alpha) * 100))


def _percentile(sorted_values: list[float], percent: float) -> float:
    """Linear-interpolation percentile, as computed by ``numpy.percentile``."""
    position = (len(sorted_values) - 1) * percent / 100
    lower, upper = math.floor(position), math.ceil(position)
    fraction = position - lower
    return (
        sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * fraction
    )


def summarize(
    group: GroupScores, n_bootstrap: int, ci_level: float, seed: int
) -> dict[str, object]:
    by_source = group.by_source()
    sources = {
        source: {
            "passed": sum(outcome.passed for outcome in outcomes),
            "total": len(outcomes),
            "pass_rate": sum(outcome.passed for outcome in outcomes) / len(outcomes),
        }
        for source, outcomes in by_source.items()
    }
    types = {
        test_type: {
            "total": len(outcomes),
            "pass_rate": sum(o.pass_ratio for o in outcomes) / len(outcomes),
        }
        for test_type, outcomes in group.by_type().items()
    }
    rates = [entry["pass_rate"] for entry in sources.values()]
    ci = bootstrap_ci(
        [[float(o.passed) for o in outcomes] for outcomes in by_source.values()],
        n_bootstrap,
        ci_level,
        seed,
    )
    return {
        "overall": sum(rates) / len(rates) if rates else 0.0,
        "ci": list(ci),
        "ci_half_width": (ci[1] - ci[0]) / 2,
        "num_tests": len(group.tests),
        "num_scored_trials": len(group.rewards),
        "harbor_mean_reward": (
            sum(group.rewards) / len(group.rewards) if group.rewards else 0.0
        ),
        "by_source": sources,
        "by_type": types,
        "unscored_trials": group.unscored_trials,
        "complete": not group.unscored_trials,
        "empty_outputs": dict(sorted(group.empty_outputs.items())),
        "test_errors": group.test_errors,
        "scored_with_exception": dict(sorted(group.scored_with_exception.items())),
        "tasks_with_several_trials": sum(
            count > 1 for count in group.trials_per_pdf.values()
        ),
    }


def format_summary(label: str, summary: dict, allow_unscored: bool) -> str:
    """Render one group in the layout of benchmark.py's final summary."""
    unscored = summary["unscored_trials"]
    if unscored and not allow_unscored:
        total = len(unscored) + summary["num_scored_trials"]
        lines = [
            (
                f"{label:20s} : Average Score: FAILED ({len(unscored)} of {total} "
                "trial(s) have no verifier result)"
            ),
            "    Run these trials again (harbor job resume with -f <ErrorType>), or pass",
            "    --allow-unscored to print the numbers without them:",
        ]
        return "\n".join(lines + [f"        {name}" for name in unscored])

    lines = [
        (
            f"{label:20s} : Average Score: {summary['overall'] * 100:0.1f}% "
            f"± {summary['ci_half_width'] * 100:0.1f}% (average of per-JSONL scores) "
            f"over {summary['num_tests']} tests"
        )
    ]
    for test_type, entry in summary["by_type"].items():
        lines.append(
            f"    {test_type:8s}: {entry['pass_rate'] * 100:0.1f}% average pass rate "
            f"over {entry['total']} tests"
        )
    lines += ["", "    Results by JSONL file:"]
    for source, entry in summary["by_source"].items():
        lines.append(
            f"        {source:30s}: {entry['pass_rate'] * 100:0.1f}% "
            f"({entry['passed']}/{entry['total']} tests)"
        )
    lines += [
        "",
        (
            "    Harbor mean reward (mean of per-task pass fractions): "
            f"{summary['harbor_mean_reward']:.4f} over "
            f"{summary['num_scored_trials']} scored trial(s)"
        ),
    ]
    if summary["empty_outputs"]:
        reasons = ", ".join(
            f"{count} {reason}" for reason, count in summary["empty_outputs"].items()
        )
        lines.append(f"    Outputs scored as an empty document: {reasons}")
    if summary["test_errors"]:
        lines.append(
            f"    WARNING: {summary['test_errors']} test(s) raised an error and were "
            "counted as failed; the original harness reports FAILED (errors) for "
            "such a candidate."
        )
    if summary["scored_with_exception"]:
        kinds = ", ".join(
            f"{count} {kind}"
            for kind, count in summary["scored_with_exception"].items()
        )
        lines.append(
            f"    Note: Harbor recorded an exception for some scored trials ({kinds}); "
            "they are scored on the output the agent left."
        )
    if summary["tasks_with_several_trials"]:
        lines.append(
            f"    Note: {summary['tasks_with_several_trials']} task(s) have several "
            "trials in this job; each test passes if most of its trials pass, as "
            "the original harness treats repeats. Use one job per run for one "
            "score per run."
        )
    if unscored:
        lines.append(
            f"    WARNING: PARTIAL RESULT. {len(unscored)} trial(s) have no verifier "
            "result and are excluded from every number above:"
        )
        lines += [f"        {name}" for name in unscored]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Recompute upstream-style olmOCR-Bench scores from a Harbor job."
    )
    parser.add_argument("job_dir", type=Path, help="Harbor job directory (jobs/<name>)")
    parser.add_argument(
        "--bootstrap-samples",
        type=int,
        default=1000,
        help="Bootstrap samples for the confidence interval (upstream default: 1000)",
    )
    parser.add_argument(
        "--confidence-level",
        type=float,
        default=0.95,
        help="Confidence level of the interval (upstream default: 0.95)",
    )
    parser.add_argument(
        "--seed", type=int, default=0, help="Seed for the bootstrap resampling"
    )
    parser.add_argument(
        "--json", type=Path, default=None, help="Also write the summary to this file"
    )
    parser.add_argument(
        "--allow-unscored",
        action="store_true",
        help="Print partial numbers when some trials have no verifier result "
        "(default: report FAILED and exit with status 1)",
    )
    args = parser.parse_args()

    try:
        groups = load_job(args.job_dir)
    except (OSError, ValueError, KeyError) as exc:
        sys.exit(f"Could not read Harbor job {args.job_dir}: {exc!r}")

    summaries = {
        label: summarize(
            group, args.bootstrap_samples, args.confidence_level, args.seed
        )
        for label, group in groups.items()
    }
    print(f"Final Summary with {args.confidence_level * 100:g}% Confidence Intervals:")
    for label, summary in summaries.items():
        print(format_summary(label, summary, args.allow_unscored))
        print()
    if args.json is not None:
        args.json.write_text(json.dumps(summaries, indent=2) + "\n", encoding="utf-8")
    if not args.allow_unscored and not all(s["complete"] for s in summaries.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
