"""
Main entry point for the template adapter. Do not modify any of the existing flags.
You can add any additional flags you need.

Constructs the Adapter class defined in adapter.py and calls run() to generate tasks
in the Harbor format at the configured output directory.
"""

import argparse
from pathlib import Path

from .adapter import AuthorizationBenchAdapter

# Default output dir: <repo>/datasets/<adapter_id>
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "datasets" / "authorizationbench"


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
        "--source-dir",
        type=Path,
        default=None,
        help="Existing AuthorizationBench checkout at the pinned commit "
        "(default: fetch it into ~/.cache/harbor-authorizationbench)",
    )
    args = parser.parse_args()

    adapter = AuthorizationBenchAdapter(
        args.output_dir,
        overwrite=args.overwrite,
        limit=args.limit,
        task_ids=args.task_ids,
        source_dir=args.source_dir,
    )

    adapter.run()


if __name__ == "__main__":
    main()
