"""Command-line entry point for the InferenceBench adapter."""

import argparse
import logging
from pathlib import Path

from .adapter import InferenceBenchAdapter

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "datasets" / "inferencebench"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory to write generated tasks",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Generate only the first N tasks",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing tasks",
    )
    parser.add_argument(
        "--task-ids",
        nargs="+",
        default=None,
        help="Only generate these task IDs",
    )
    parser.add_argument(
        "--upstream-dir",
        type=Path,
        default=None,
        help="Use this InferenceBench checkout instead of fetching the pinned commit",
    )
    parser.add_argument(
        "--num-hours",
        default="2",
        help="Agent time budget in hours (the paper's main results use 2)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    adapter = InferenceBenchAdapter(
        args.output_dir,
        overwrite=args.overwrite,
        limit=args.limit,
        task_ids=args.task_ids,
        upstream_dir=args.upstream_dir,
        num_hours=args.num_hours,
    )

    adapter.run()


if __name__ == "__main__":
    main()
