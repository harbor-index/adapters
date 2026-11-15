"""CLI entry point for the CWE-bench adapter.

Constructs the adapter defined in adapter.py and calls run() to write Harbor task
directories for the pinned CWE-bench release into the output directory.
"""

import argparse
import logging
import sys
from pathlib import Path

from .adapter import (
    DEFAULT_RELEASE,
    UPSTREAM_RELEASES,
    CWEBenchAccessError,
    CWEBenchAdapter,
    CWEBenchSourceError,
)

# Default output dir: <repo>/datasets/cwe-bench
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "datasets" / "cwe-bench"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate Harbor task directories for CWE-bench."
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
        help="Only generate these task IDs",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--revision",
        default=DEFAULT_RELEASE,
        help="Upstream release ("
        + ", ".join(UPSTREAM_RELEASES)
        + ") or a Hugging Face revision: commit hash, tag or branch "
        f"(default: {DEFAULT_RELEASE})",
    )
    source.add_argument(
        "--source-dir",
        type=Path,
        default=None,
        help="Local copy of the upstream repository (the directory containing "
        "tasks/), used as it is instead of downloading a release",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Where to download the upstream snapshot "
        "(default: $CWE_BENCH_CACHE_DIR or ~/.cache/harbor-adapters/cwe-bench); "
        "not allowed with --source-dir",
    )
    parser.add_argument(
        "--omit-solutions",
        action="store_true",
        help="Write a stub instead of each upstream reference solution. A directory "
        "generated without this flag can only be converted as a whole (--overwrite "
        "without --limit/--task-ids)",
    )
    args = parser.parse_args()
    if args.source_dir is not None and args.cache_dir is not None:
        parser.error("argument --cache-dir: not allowed with argument --source-dir")

    # Show this adapter's progress; keep third-party request logging quiet.
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    logging.getLogger("cwe_bench").setLevel(logging.INFO)

    adapter = CWEBenchAdapter(
        args.output_dir,
        overwrite=args.overwrite,
        limit=args.limit,
        task_ids=args.task_ids,
        revision=args.revision,
        source_dir=args.source_dir,
        cache_dir=args.cache_dir,
        omit_solutions=args.omit_solutions,
    )

    try:
        adapter.run()
    except (CWEBenchAccessError, CWEBenchSourceError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
