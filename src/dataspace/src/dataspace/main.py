"""
Main entry point for the DataSpace adapter. Do not modify any of the existing flags.
You can add any additional flags you need.

Constructs the Adapter class defined in adapter.py and calls run() to generate tasks
in the Harbor format at the configured output directory.
"""

import argparse
from pathlib import Path

from .adapter import DEFAULT_CACHE_DIR, DataSpaceAdapter

# Default output dir: <repo>/datasets/<adapter_id>
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "datasets" / "dataspace"


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
        help="Only generate these task IDs (e.g. task_10 or 10)",
    )
    parser.add_argument(
        "--benchmark-root",
        type=Path,
        default=None,
        help="Existing extracted DataSpace-Benchmark directory; skips the download",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_CACHE_DIR,
        help=f"Download and extraction cache (default: {DEFAULT_CACHE_DIR})",
    )
    args = parser.parse_args()

    adapter = DataSpaceAdapter(
        args.output_dir,
        overwrite=args.overwrite,
        limit=args.limit,
        task_ids=args.task_ids,
        benchmark_root=args.benchmark_root,
        cache_dir=args.cache_dir,
    )

    generated = adapter.run()
    print(f"Generated {len(generated)} DataSpace task(s) in {args.output_dir}")


if __name__ == "__main__":
    main()
