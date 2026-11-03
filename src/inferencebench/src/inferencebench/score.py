"""Compute the paper's Table 2 numbers from Harbor job directories or upstream result files."""

import argparse
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from statistics import mean

TASK_RE = re.compile(r"inferencebench-(?P<scenario>[a-d])-[a-z0-9-]+?-(?P<agent>\d+)-(?P<eval>\d+)")
SCENARIOS = ("a", "b", "c", "d")


def sample_sem(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return float("nan")
    center = mean(values)
    return math.sqrt(sum((value - center) ** 2 for value in values) / (n * (n - 1)))


def geomean(values: list[float]) -> float:
    return math.exp(mean(math.log(value) for value in values))


def load_rewards(job_dir: Path) -> list[dict]:
    """Return one record per finished trial under job_dir."""
    records = []
    for path in sorted(job_dir.rglob("result.json")):
        result = json.loads(path.read_text(encoding="utf-8"))
        match = TASK_RE.search(result.get("task_name") or "")
        rewards = (result.get("verifier_result") or {}).get("rewards")
        if match is None or not rewards:
            continue
        records.append(
            {
                "scenario": match["scenario"],
                "seed_pair": f"{match['agent']}:{match['eval']}",
                "rewards": rewards,
            }
        )
    return records


def summarize(records: list[dict]) -> dict:
    """Aggregate as Table 2 does: means per scenario, then their geometric mean.

    The SEM of the aggregate is computed over seed pairs, from each seed pair's geometric mean
    over scenarios.
    """
    by_scenario: dict[str, list[float]] = defaultdict(list)
    by_seed: dict[str, dict[str, float]] = defaultdict(dict)
    for record in records:
        reward = float(record["rewards"]["reward"])
        by_scenario[record["scenario"]].append(reward)
        by_seed[record["seed_pair"]][record["scenario"]] = reward
    summary = {
        "n_trials": len(records),
        "n_valid": sum(int(record["rewards"].get("valid", 0)) for record in records),
        "n_judge_invalid": sum(1 - int(record["rewards"].get("judge_valid", 0)) for record in records),
        "scenarios": {
            scenario: {
                "mean": mean(values),
                "sem": sample_sem(values),
                "runs": values,
            }
            for scenario, values in sorted(by_scenario.items())
        },
    }
    if all(scenario in by_scenario for scenario in SCENARIOS):
        seeds = [
            geomean([values[scenario] for scenario in SCENARIOS])
            for values in by_seed.values()
            if all(scenario in values for scenario in SCENARIOS)
        ]
        summary["aggregate"] = {
            "mean": geomean([mean(by_scenario[scenario]) for scenario in SCENARIOS]),
            "sem": sample_sem(seeds),
            "seed_pairs": seeds,
        }
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_dirs", nargs="+", type=Path, help="Harbor job directories")
    args = parser.parse_args()
    for job_dir in args.job_dirs:
        print(json.dumps({"job": str(job_dir), **summarize(load_rewards(job_dir))}, indent=2))


if __name__ == "__main__":
    main()
