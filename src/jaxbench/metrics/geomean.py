"""Harbor uv-script metric: JAXBench geomean speedup (arXiv:2607.20466, Table 2).

Reads one reward dict per trial (JSONL, `null` for trials without a reward) and
writes the geometric mean of the floored per-task speedup (`reward`). Trials
without a reward count as 1x, matching the paper's treatment of incorrect
benchmarks.
"""

import argparse
import json
import math


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("-i", "--input", required=True)
    parser.add_argument("-o", "--output", required=True)
    args = parser.parse_args()

    speedups: list[float] = []
    correct = 0
    with open(args.input) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            reward = json.loads(line)
            if reward is None:
                speedups.append(1.0)
                continue
            speedups.append(max(float(reward.get("reward", 1.0)), 1.0))
            correct += int(reward.get("correct", 0))

    n = len(speedups)
    geomean = math.exp(sum(math.log(s) for s in speedups) / n) if n else 0.0
    with open(args.output, "w") as f:
        json.dump(
            {
                "geomean_speedup": round(geomean, 4),
                "mean_speedup": round(sum(speedups) / n, 4) if n else 0.0,
                "n_correct": correct,
                "n_tasks": n,
            },
            f,
        )


if __name__ == "__main__":
    main()
