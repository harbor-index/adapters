"""Compare native-policy parity runs from the original side and the Harbor side.

Original runs are the JSON files written by `agenteval_games.original_runner`;
Harbor runs are job directories written by `harbor run`. A run counts only if
every selected episode finished without an error, so an infrastructure failure
is never scored as a loss. Scores are percentages of the final-step reward.
"""

import argparse
import json
import math
from pathlib import Path

GAMES = ("maze", "wordle")


def game_of(task_id: str) -> str:
    (game,) = [name for name in GAMES if "-" + name + "-" in task_id]
    return game


def load_original(path: Path) -> dict[str, float]:
    report = json.loads(path.read_text())
    if report["status"] != "completed":
        raise ValueError(str(path) + " did not complete: " + str(report["status"]))
    rewards = {}
    for episode in report["episodes"]:
        if episode["status"] != "completed" or episode["reward"] is None:
            raise ValueError(str(path) + ": unscored " + episode["task_id"])
        rewards[episode["task_id"]] = float(episode["reward"])
    if sorted(rewards) != sorted(report["selected_task_ids"]):
        raise ValueError(str(path) + " is missing selected episodes")
    return rewards


def load_harbor(job_dir: Path) -> dict[str, float]:
    rewards = {}
    for path in sorted(job_dir.glob("*/result.json")):
        trial = json.loads(path.read_text())
        task_id = trial["task_name"].rsplit("/", 1)[-1]
        if trial.get("exception_info"):
            raise ValueError(str(job_dir) + ": " + task_id + " raised an exception")
        reward = float(trial["verifier_result"]["rewards"]["reward"])
        # The agent's own record of the episode must agree with the verifier.
        agent = json.loads((path.parent / "agent/agenteval-native.json").read_text())
        if agent["status"] != "completed" or float(agent["reward"]) != reward:
            raise ValueError(
                str(job_dir) + ": agent and verifier disagree on " + task_id
            )
        if task_id in rewards:
            raise ValueError(str(job_dir) + ": more than one trial for " + task_id)
        rewards[task_id] = reward
    if not rewards:
        raise ValueError(str(job_dir) + " has no trial results")
    return rewards


def score(rewards: dict[str, float], game: str | None = None) -> float:
    values = [v for task, v in rewards.items() if game in (None, game_of(task))]
    return 100.0 * sum(values) / len(values)


def mean_sem(runs: list[float]) -> tuple[float, float]:
    """Mean and sample standard error of the mean; needs at least two runs."""
    mean = sum(runs) / len(runs)
    if len(runs) < 2:
        return mean, math.nan
    variance = sum((x - mean) ** 2 for x in runs) / (len(runs) - 1)
    return mean, math.sqrt(variance / len(runs))


def ranges_overlap(a: list[float], b: list[float]) -> bool:
    return max(a) >= min(b) and max(b) >= min(a)


def compare(original: list[dict[str, float]], harbor: list[dict[str, float]]) -> dict:
    tasks = sorted(original[0])
    for run in original + harbor:
        if sorted(run) != tasks:
            raise ValueError("Runs do not cover the same tasks")
    metrics = []
    for label, game in (("all", None), *((game, game) for game in GAMES)):
        if game and not any(game_of(task) == game for task in tasks):
            continue
        sides = {}
        for side, runs in (("original", original), ("harbor", harbor)):
            scores = [round(score(run, game), 4) for run in runs]
            mean, sem = mean_sem(scores)
            sides[side + "_runs"] = scores
            sides[side] = "%.2f ± %.2f" % (mean, sem)
        metrics.append(
            {
                "subset": label,
                "tasks": sum(1 for task in tasks if game in (None, game_of(task))),
                **sides,
                "ranges_overlap": ranges_overlap(
                    sides["original_runs"], sides["harbor_runs"]
                ),
            }
        )
    differing = {}
    for task in tasks:
        row = {
            "original": [run[task] for run in original],
            "harbor": [run[task] for run in harbor],
        }
        if len({*row["original"], *row["harbor"]}) > 1:
            differing[task] = row
    return {
        "tasks": len(tasks),
        "runs_per_side": {"original": len(original), "harbor": len(harbor)},
        "metrics": metrics,
        "tasks_with_differing_rewards": differing,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original", type=Path, nargs="+", required=True)
    parser.add_argument("--harbor", type=Path, nargs="+", required=True)
    parser.add_argument("--output-json", type=Path)
    args = parser.parse_args()
    result = compare(
        [load_original(path) for path in args.original],
        [load_harbor(path) for path in args.harbor],
    )
    print("| Subset | Tasks | Original (mean ± SEM) | Harbor (mean ± SEM) | Overlap |")
    print("|--------|-------|-----------------------|---------------------|---------|")
    for row in result["metrics"]:
        print(
            "| %s | %d | %s %s | %s %s | %s |"
            % (
                row["subset"],
                row["tasks"],
                row["original"],
                row["original_runs"],
                row["harbor"],
                row["harbor_runs"],
                "yes" if row["ranges_overlap"] else "NO",
            )
        )
    differing = result["tasks_with_differing_rewards"]
    print("Tasks whose reward differs between any two runs:", len(differing))
    for task, row in differing.items():
        print(" ", task, "original", row["original"], "harbor", row["harbor"])
    if args.output_json:
        args.output_json.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
