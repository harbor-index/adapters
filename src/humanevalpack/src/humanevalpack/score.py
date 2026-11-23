"""pass@1 per language and per task family from a Harbor job directory.

The OctoPack paper reports pass@1 separately for every language of
HumanEvalFix and HumanEvalSynthesize, and the average over the six languages.
Harbor reports one mean reward over all trials of a job, which mixes languages
and task families. This script rebuilds the per-language numbers from the
``result.json`` that each task's verifier writes.

    uv run humanevalpack-score jobs/<job-name>

A trial without a verifier result (the environment did not start, scoring
broke or timed out, the agent raised before the verifier ran) has no score,
and neither has a trial that the job planned but has not finished. Leaving
such trials out would make a job look better than it is, so the script then
says which trials are unscored and exits with status 1 instead of printing
scores. ``--allow-unscored`` prints the partial numbers.

If a job holds several trials of one problem (``harbor run -k``), the score of
that problem is the fraction of its trials that passed, and pass@1 is the mean
of these fractions over the problems. Use one job per run to get one score per
run.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from . import upstream

RESULT_FILENAME = "result.json"
REWARD_FILENAME = "reward.txt"
# Upstream task prefixes in the order of the adapter's subsets.
FAMILIES = ("humanevalfixtests", "humanevalfixdocs", "humanevalsynthesize")


class JobError(Exception):
    """The path is not a readable Harbor job directory."""


def read_trial(trial_dir: Path) -> dict | None:
    """The verifier's record of one trial, or None if the trial has no usable score."""
    result_path = trial_dir / "verifier" / RESULT_FILENAME
    reward_path = trial_dir / "verifier" / REWARD_FILENAME
    try:
        record = json.loads(result_path.read_text(encoding="utf-8"))
        reward = float(reward_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or not isinstance(record.get("passed"), bool):
        return None
    family, _, language = str(record.get("upstream_task", "")).partition("-")
    if family not in FAMILIES or language not in upstream.LANGUAGES:
        return None
    # The reward file and the verifier's record are written by different steps;
    # a trial in which they disagree was not scored properly.
    if reward != (1.0 if record["passed"] else 0.0):
        return None
    return {
        "family": family,
        "language": language,
        "task_id": str(record.get("task_id")),
        "passed": record["passed"],
        "submission_status": str(record.get("submission_status", "ok")),
    }


def agent_label(trial: dict) -> str:
    agent = trial.get("agent_info") or {}
    label = agent.get("name") or "unknown-agent"
    model = agent.get("model_info") or {}
    if model.get("name"):
        label += f" ({model['name']})"
    return label


def planned_trials(job_dir: Path) -> int | None:
    """The number of trials the job set out to run, from its own result file."""
    try:
        job = json.loads((job_dir / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    total = job.get("n_total_trials") if isinstance(job, dict) else None
    return total if isinstance(total, int) and not isinstance(total, bool) else None


def load_job(job_dir: Path) -> dict[str, dict]:
    """Read every trial of one Harbor job, grouped by "agent (model)"."""
    if not job_dir.is_dir():
        raise JobError(f"{job_dir} is not a directory")
    result_paths = sorted(job_dir.glob("*/result.json"))
    if not result_paths:
        raise JobError(
            f"no trial directories with a result.json in {job_dir}; pass one job "
            "directory (jobs/<job-name>)"
        )

    groups: dict[str, dict] = defaultdict(lambda: {"records": [], "unscored": []})
    for result_path in result_paths:
        try:
            trial = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise JobError(f"cannot read {result_path}: {error}") from error
        if not isinstance(trial, dict) or "trial_name" not in trial:
            raise JobError(
                f"{result_path} is not a trial result; pass one job directory "
                "(jobs/<job-name>), not a directory that holds several jobs"
            )
        group = groups[agent_label(trial)]
        record = read_trial(result_path.parent)
        if record is None:
            group["unscored"].append(result_path.parent.name)
        else:
            group["records"].append(record)
    return dict(sorted(groups.items()))


def summarize(group: dict) -> dict:
    """pass@1 per (family, language): the mean over problems of the fraction of passing trials."""
    per_task: dict[tuple[str, str], dict[str, list[bool]]] = defaultdict(
        lambda: defaultdict(list)
    )
    statuses: dict[str, int] = defaultdict(int)
    for record in group["records"]:
        per_task[(record["family"], record["language"])][record["task_id"]].append(
            record["passed"]
        )
        statuses[record["submission_status"]] += 1

    families: dict[str, dict] = {}
    for family in FAMILIES:
        languages = {}
        for language in upstream.LANGUAGES:
            tasks = per_task.get((family, language))
            if not tasks:
                continue
            rates = [sum(outcomes) / len(outcomes) for outcomes in tasks.values()]
            languages[language] = {
                "tasks": len(tasks),
                "trials": sum(len(outcomes) for outcomes in tasks.values()),
                "passed_trials": sum(sum(outcomes) for outcomes in tasks.values()),
                "pass_at_1": sum(rates) / len(rates),
            }
        if languages:
            families[family] = {
                "languages": languages,
                # The paper's "Avg." column: the unweighted mean over languages.
                "mean_over_languages": sum(
                    entry["pass_at_1"] for entry in languages.values()
                )
                / len(languages),
                "tasks": sum(entry["tasks"] for entry in languages.values()),
            }
    records = group["records"]
    return {
        "families": families,
        "scored_trials": len(records),
        "mean_reward": sum(record["passed"] for record in records) / len(records)
        if records
        else None,
        "submission_status": dict(sorted(statuses.items())),
        "unscored_trials": group["unscored"],
        "complete": not group["unscored"],
    }


def format_summary(label: str, summary: dict, allow_unscored: bool) -> str:
    unscored = summary["unscored_trials"]
    if unscored and not allow_unscored:
        total = len(unscored) + summary["scored_trials"]
        lines = [
            f"{label}: NO SCORE ({len(unscored)} of {total} trial(s) have no verifier result)",
            "  Run these trials again (harbor job resume), or pass --allow-unscored to print",
            "  the numbers without them:",
        ]
        return "\n".join(lines + [f"    {name}" for name in unscored])

    lines = [f"{label}: {summary['scored_trials']} scored trial(s)"]
    for family, entry in summary["families"].items():
        lines.append(f"  {family} (pass@1 in %, {entry['tasks']} problems)")
        for language, numbers in entry["languages"].items():
            lines.append(
                f"    {language:7s} {numbers['pass_at_1'] * 100:6.2f}   "
                f"({numbers['passed_trials']}/{numbers['trials']} trials passed, {numbers['tasks']} problems)"
            )
        lines.append(
            f"    {'mean':7s} {entry['mean_over_languages'] * 100:6.2f}   "
            f"(unweighted mean over {len(entry['languages'])} language(s))"
        )
    if summary["mean_reward"] is not None:
        lines.append(
            f"  Harbor mean reward over all scored trials: {summary['mean_reward']:.4f}"
        )
    other = {
        status: count
        for status, count in summary["submission_status"].items()
        if status != "ok"
    }
    if other:
        text = ", ".join(f"{count} {status}" for status, count in other.items())
        lines.append(f"  Submissions scored as the empty generation: {text}")
    if unscored:
        lines.append(
            f"  WARNING: PARTIAL RESULT. {len(unscored)} trial(s) have no verifier result and are "
            "excluded from every number above:"
        )
        lines += [f"    {name}" for name in unscored]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="humanevalpack-score",
        description="pass@1 per language and task family from one Harbor job directory.",
    )
    parser.add_argument(
        "job_dir", type=Path, help="Harbor job directory (jobs/<job-name>)"
    )
    parser.add_argument(
        "--json", type=Path, default=None, help="Also write the summary to this file"
    )
    parser.add_argument(
        "--allow-unscored",
        action="store_true",
        help="Print partial numbers when some trials have no verifier result "
        "(default: list them and exit with status 1)",
    )
    args = parser.parse_args()

    try:
        groups = load_job(args.job_dir)
    except JobError as error:
        sys.exit(f"error: {error}")

    summaries = {label: summarize(group) for label, group in groups.items()}

    # Trials the job planned but that have no result yet (the job was
    # interrupted or is still running) are missing from every group.
    planned = planned_trials(args.job_dir)
    finished = sum(
        summary["scored_trials"] + len(summary["unscored_trials"])
        for summary in summaries.values()
    )
    pending = max(planned - finished, 0) if planned is not None else 0
    if pending and not args.allow_unscored:
        sys.exit(
            f"NO SCORE: the job is incomplete, {pending} of {planned} planned trial(s) "
            "have no result yet. Finish it (harbor job resume), or pass "
            "--allow-unscored to print the numbers without them."
        )

    for label, summary in summaries.items():
        print(format_summary(label, summary, args.allow_unscored))
        print()
    if pending:
        print(
            f"WARNING: PARTIAL RESULT. {pending} of {planned} planned trial(s) have no "
            "result yet and are in none of the numbers above."
        )
        print()
    if args.json is not None:
        try:
            args.json.write_text(
                json.dumps(summaries, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as error:
            sys.exit(f"error: cannot write {args.json}: {error}")
    if not args.allow_unscored and not all(
        summary["complete"] for summary in summaries.values()
    ):
        sys.exit(1)


if __name__ == "__main__":
    main()
