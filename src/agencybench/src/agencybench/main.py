"""CLI entry point for the AgencyBench V2 Harbor adapter."""

from __future__ import annotations

import argparse
from pathlib import Path

from .adapter import AgencyBenchAdapter

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "datasets" / "agencybench"


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="agencybench",
        description="Generate Harbor tasks for AgencyBench V2 scenarios.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory to write generated Harbor tasks.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Generate only the first N tasks from the selected split.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing generated task directories.",
    )
    parser.add_argument(
        "--task-ids",
        nargs="+",
        default=None,
        help="Generate only these task IDs (Harbor IDs or upstream scenario IDs).",
    )
    parser.add_argument(
        "--split",
        choices=("pilot", "parity", "implemented", "all"),
        default="implemented",
        help="Scenario split to generate (default: implemented).",
    )
    parser.add_argument(
        "--upstream-dir",
        type=Path,
        default=None,
        help="Path to a local checkout of GAIR-NLP/AgencyBench.",
    )
    args = parser.parse_args()

    adapter = AgencyBenchAdapter(
        output_dir=args.output_dir,
        overwrite=args.overwrite,
        limit=args.limit,
        task_ids=args.task_ids,
        split=args.split,
        upstream_dir=args.upstream_dir,
    )
    generated = adapter.run()
    print(f"Generated {len(generated)} AgencyBench task(s) in {args.output_dir}")
    for path in generated:
        print(f"  - {path.name}")


if __name__ == "__main__":
    main()
