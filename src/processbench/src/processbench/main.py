"""
Main entry point for the template adapter. Do not modify any of the existing flags.
You can add any additional flags you need.

Constructs the Adapter class defined in adapter.py and calls run() to generate tasks
in the Harbor format at the configured output directory.
"""

import argparse
from pathlib import Path

from .adapter import SUBSETS, ProcessBenchAdapter

# Default output dir: <repo>/datasets/<adapter_id>
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "datasets" / "processbench"


def main() -> None:
    parser = argparse.ArgumentParser()
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
        "--split",
        choices=["full", "parity"],
        default="full",
        help="full: all 3,400 items; parity: stratified 50-per-subset sample",
    )
    parser.add_argument(
        "--subsets",
        nargs="+",
        choices=SUBSETS,
        default=None,
        help="ProcessBench subsets to include (default: all four)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Directory with local <subset>.json files (default: download from HF)",
    )
    args = parser.parse_args()

    adapter = ProcessBenchAdapter(
        args.output_dir,
        overwrite=args.overwrite,
        limit=args.limit,
        task_ids=args.task_ids,
        data_dir=args.data_dir,
        subsets=args.subsets,
        split=args.split,
    )

    adapter.run()


if __name__ == "__main__":
    main()
