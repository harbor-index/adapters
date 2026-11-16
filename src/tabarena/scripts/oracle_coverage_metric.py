# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Harbor 0.26 uv-script metric for the complete 51-task Lite release.

This reports oracle coverage, never a mean of heterogeneous native errors.
An explicit expected count prevents missing trials from producing apparent 100%.
For subsets, set TABARENA_EXPECTED_TASKS to the planned task count.
Reward inputs contain no task IDs: a separate manifest-to-trial identity and
exception audit remains mandatory before claiming all included tasks passed.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path

DEFAULT_EXPECTED_TASKS = 51


def is_number(value):
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def summarize(rewards, expected_tasks=DEFAULT_EXPECTED_TASKS):
    if type(expected_tasks) is not int or expected_tasks < 1:
        raise ValueError("Expected task count must be a positive integer")
    scored = [
        item
        for item in rewards
        if isinstance(item, dict)
        and is_number(item.get("native_error"))
        and is_number(item.get("metric_optimal"))
        and item["metric_optimal"] in (0, 1)
    ]
    optimum = sum(item["metric_optimal"] == 1 for item in scored)
    count_valid = len(rewards) <= expected_tasks
    return {
        "expected_trials": expected_tasks,
        "received_reward_records": len(rewards),
        "scored_trials": len(scored),
        "exact_optimum_trials": optimum,
        "exact_optimum_fraction": optimum / expected_tasks if count_valid else 0.0,
        "complete_scored_count": int(len(rewards) == len(scored) == expected_tasks),
        "excess_reward_records": max(0, len(rewards) - expected_tasks),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-i", "--input", required=True, type=Path)
    parser.add_argument("-o", "--output", required=True, type=Path)
    parser.add_argument(
        "--expected-tasks",
        type=int,
        default=os.environ.get("TABARENA_EXPECTED_TASKS", DEFAULT_EXPECTED_TASKS),
    )
    args = parser.parse_args()
    rewards = [
        json.loads(line) for line in args.input.read_text().splitlines() if line.strip()
    ]
    args.output.write_text(
        json.dumps(summarize(rewards, args.expected_tasks), allow_nan=False) + "\n"
    )


if __name__ == "__main__":
    main()
