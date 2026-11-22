"""Command-line entry point of the TavernBench adapter.

Builds :class:`TavernBenchAdapter` from the standard adapter flags and calls
``run()`` to write Harbor tasks to the output directory.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .adapter import DEFAULT_SPLIT, SPLITS, TavernBenchAdapter

# Default output dir: <repo>/datasets/tavernbench
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "datasets" / "tavernbench"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate Harbor tasks for TavernBench"
    )
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
        help="Only generate these tasks (upstream ids such as R1-000, task ids such as r1-000, or full task names)",
    )
    parser.add_argument(
        "--split",
        choices=SPLITS,
        default=DEFAULT_SPLIT,
        help="eval: the 368 tasks every system answers in the paper (default); full: all 960 tasks",
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=None,
        help="Root of a local AutoTavern checkout; by default the pinned files are downloaded from GitHub",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Directory for downloaded source files (default: ~/.cache/harbor-adapters/tavernbench)",
    )
    args = parser.parse_args()

    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be >= 1")

    adapter = TavernBenchAdapter(
        args.output_dir,
        overwrite=args.overwrite,
        limit=args.limit,
        task_ids=args.task_ids,
        split=args.split,
        source_dir=args.source_dir,
        cache_dir=args.cache_dir,
    )

    try:
        adapter.run()
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        # Unknown task ids, a source that does not match the pinned release, or
        # a failed download: report the reason without a traceback.
        parser.exit(2, f"tavernbench: error: {exc}\n")


if __name__ == "__main__":
    main()
