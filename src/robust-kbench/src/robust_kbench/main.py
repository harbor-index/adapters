"""
Command-line entrypoint for generating Harbor tasks from the Robust-KBench benchmark.
"""

import argparse
import logging
import sys
from pathlib import Path

# Support running directly or as a package
if __package__ in (None, ""):
    package_src = Path(__file__).resolve().parents[1]
    if str(package_src) not in sys.path:
        sys.path.insert(0, str(package_src))
    from robust_kbench.adapter import RobustKBenchAdapter
else:
    from .adapter import RobustKBenchAdapter

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("robust-kbench")


def parse_args(args=None):
    parser = argparse.ArgumentParser(
        description="Generate Harbor tasks from Sakana AI's Robust-KBench benchmark."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory where generated Harbor task folders will be written.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of tasks to generate.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing task directories.",
    )
    parser.add_argument(
        "--task-ids",
        nargs="+",
        default=None,
        help="List of specific task IDs to generate (e.g. layernorm__forward).",
    )
    parser.add_argument(
        "--include-h100-only",
        action="store_true",
        help="Include tasks whose upstream golden kernel requires >100KB shared memory (H100-only).",
    )
    return parser.parse_args(args)


def main():
    args = parse_args()
    adapter = RobustKBenchAdapter(
        output_dir=args.output_dir,
        limit=args.limit,
        overwrite=args.overwrite,
        task_ids=args.task_ids,
        include_h100_only=args.include_h100_only,
    )
    tasks = adapter.run()
    logger.info(f"Done! Generated {len(tasks)} tasks to {args.output_dir}")


if __name__ == "__main__":
    main()
