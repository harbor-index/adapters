"""Command-line entry point: `uv run agenticdatabench --output-dir <path>`."""

import argparse
import logging
from pathlib import Path

from .adapter import AgenticDataBenchAdapter

# Default output dir: <repo>/datasets/<adapter_id>
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "datasets" / "agenticdatabench"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Directory to write generated tasks")
    parser.add_argument("--limit", type=int, default=None, help="Generate only the first N tasks")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing tasks")
    parser.add_argument("--task-ids", nargs="+", default=None, help="Only generate these tasks (upstream ids, e.g. agriculture_03)")
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=None,
        help="Use a local checkout of the AgenticDataBench GitHub repo (at the pinned commit) instead of fetching it",
    )
    parser.add_argument(
        "--include-excluded",
        action="store_true",
        help="Also generate the tasks in excluded_tasks.json (for checking the exclusions)",
    )
    parser.add_argument(
        "--split",
        choices=["full", "parity"],
        default="full",
        help="full: all 229 kept tasks; parity: the 50-task parity subset (by domain, seed 0)",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    AgenticDataBenchAdapter(
        args.output_dir,
        limit=args.limit,
        overwrite=args.overwrite,
        task_ids=args.task_ids,
        source_dir=args.source_dir,
        include_excluded=args.include_excluded,
        split=args.split,
    ).run()


if __name__ == "__main__":
    main()
