#!/usr/bin/env python3
"""Replay LatchBio's published agent answers through the Harbor verifier.

scBench and SpatialBench publish, for every public eval, the answers real agents
submitted (``eval_answer.json``) together with the verdict LatchBio's harness
recorded for them (``result.json``). This script regrades each published answer
with the verifier that ships in the Harbor tasks (``tests/grade.py`` plus the
graders of the pinned latch-eval-tools release) and compares the outcome with
the recorded verdict.

This is an *evaluator replay*: it shows that, given the same answer, the Harbor
verifier reaches the verdict upstream recorded. It is not a parity experiment
(no agent is run) and does not replace one.

    python scripts/replay_upstream_results.py --upstream-root /path/to/clones

``--upstream-root`` must contain full clones of ``scbench`` and ``spatialbench``
at the commits pinned in ``latchbio.sources`` (the adapter's own cache holds only
the eval definitions, not the trajectories).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ADAPTER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADAPTER_ROOT / "src"))

from latchbio.oracle import load_grade_module  # noqa: E402
from latchbio.sources import BENCHMARKS_BY_KEY  # noqa: E402
from latchbio.upstream import ensure_checkout, load_evals  # noqa: E402

REPLAYABLE = ("scbench", "spatialbench")


def recorded_expected(grader_result: dict[str, Any]) -> dict[str, Any]:
    """Ground-truth values the upstream grader reported at the time of the run."""
    metrics = grader_result.get("metrics") or {}
    return {
        key.removesuffix("_expected"): value
        for key, value in metrics.items()
        if key.endswith("_expected")
    }


def config_ground_truth(eval_config: dict[str, Any]) -> dict[str, Any]:
    grader = eval_config.get("grader") or {}
    return dict((grader.get("config") or {}).get("ground_truth") or {})


def replay(upstream_root: Path) -> dict[str, Any]:
    grade = load_grade_module()
    rows: list[dict[str, Any]] = []

    for key in REPLAYABLE:
        source = BENCHMARKS_BY_KEY[key]
        checkout = ensure_checkout(source, cache_dir=upstream_root, upstream_root=upstream_root)
        evals = {item.eval_id: item for item in load_evals(source, checkout)}
        trajectories = checkout / "trajectories"
        if not trajectories.is_dir():
            raise SystemExit(f"{checkout} has no trajectories/ directory (need a full clone)")

        for result_path in sorted(trajectories.rglob("result.json")):
            record = json.loads(result_path.read_text())
            eval_id = record.get("task")
            item = evals.get(eval_id)
            if item is None:
                continue
            result = record.get("result") or {}
            grader_result = result.get("grader_result") or {}
            eval_config = {"id": eval_id, "grader": item.raw["grader"]}

            answer = result.get("agent_answer")
            answer_file = result_path.with_name("eval_answer.json")
            if answer is None and answer_file.is_file():
                try:
                    answer = json.loads(answer_file.read_text())
                except json.JSONDecodeError:
                    answer = None

            report = grade.grade_answer(eval_config, answer)
            # Upstream counts an ungraded evaluation (passed is None) as not passed.
            upstream_passed = result.get("passed") is True

            # The public eval definitions were revised after some runs. When the
            # ground truth recorded with the run differs from today's config, a
            # verdict change reflects that revision, not the grader.
            expected_then = recorded_expected(grader_result)
            expected_now = config_ground_truth(eval_config)
            same_ground_truth = not expected_then or all(
                expected_now.get(field) == value for field, value in expected_then.items()
            )

            rows.append(
                {
                    "benchmark": key,
                    "eval_id": eval_id,
                    "run": result_path.parent.relative_to(trajectories).as_posix(),
                    "harness": record.get("harness"),
                    "model": record.get("model_name"),
                    "latch_eval_tools_version": record.get("latch_eval_tools_version"),
                    "answered": answer is not None,
                    "upstream_passed": upstream_passed,
                    "harbor_passed": report["passed"],
                    "same_ground_truth": same_ground_truth,
                    "agree": upstream_passed == report["passed"],
                }
            )

    comparable = [row for row in rows if row["same_ground_truth"]]
    by_benchmark: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        counter = by_benchmark[row["benchmark"]]
        counter["runs"] += 1
        counter["answered"] += row["answered"]
        counter["upstream_passed"] += row["upstream_passed"]
        counter["harbor_passed"] += row["harbor_passed"]
        counter["ground_truth_revised"] += not row["same_ground_truth"]
        if row["same_ground_truth"]:
            counter["comparable"] += 1
            counter["agree"] += row["agree"]

    return {
        "summary": {
            "runs": len(rows),
            "comparable_runs": len(comparable),
            "agreeing_runs": sum(row["agree"] for row in comparable),
            "disagreeing_runs": sum(not row["agree"] for row in comparable),
            "runs_with_revised_ground_truth": len(rows) - len(comparable),
            "evals": len({(row["benchmark"], row["eval_id"]) for row in rows}),
        },
        "by_benchmark": {key: dict(counter) for key, counter in by_benchmark.items()},
        "disagreements": [row for row in comparable if not row["agree"]],
        "revised_ground_truth_evals": sorted(
            {f"{row['benchmark']}/{row['eval_id']}" for row in rows if not row["same_ground_truth"]}
        ),
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--upstream-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="Write the full report as JSON")
    args = parser.parse_args()

    report = replay(args.upstream_root)
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + "\n")

    summary = report["summary"]
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, indent=2))
    print(
        f"\n{summary['agreeing_runs']}/{summary['comparable_runs']} comparable "
        f"published runs get the same verdict from the Harbor verifier "
        f"({summary['runs_with_revised_ground_truth']} more runs predate a "
        "ground-truth revision and are not comparable)."
    )
    return 1 if summary["disagreeing_runs"] else 0


if __name__ == "__main__":
    sys.exit(main())
