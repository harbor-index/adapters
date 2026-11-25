"""CLI entry point: ``uv run cmphysbench --output-dir <dir>``."""

import argparse
import logging
import sys
from pathlib import Path

from .adapter import CMPhysBenchAdapter

# Default output dir: <repo>/datasets/cmphysbench
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "datasets" / "cmphysbench"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="Generate Harbor tasks for CMPhysBench")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory to write generated tasks (default: datasets/cmphysbench)",
    )
    parser.add_argument("--limit", type=int, default=None, help="Generate only the first N tasks")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing tasks")
    parser.add_argument(
        "--task-ids",
        nargs="+",
        default=None,
        help="Only generate these task IDs (e.g. 7 or cmphysbench-7)",
    )
    parser.add_argument(
        "--data-file",
        type=Path,
        default=None,
        help="Local CMPhysBench.json (default: download the pinned revision)",
    )
    parser.add_argument(
        "--seed-repo-dir",
        type=Path,
        default=None,
        help="Local CMPhysBench/CMPhysBench checkout (default: download the pinned SEED files)",
    )
    args = parser.parse_args()

    adapter = CMPhysBenchAdapter(
        args.output_dir,
        overwrite=args.overwrite,
        limit=args.limit,
        task_ids=args.task_ids,
        data_file=args.data_file,
        seed_repo_dir=args.seed_repo_dir,
    )
    try:
        adapter.run()
    except (OSError, RuntimeError, ValueError) as exc:
        logging.error("%s", exc)
        sys.exit(1)
    if adapter.missing_task_ids or adapter.failed_task_ids:
        sys.exit(1)


if __name__ == "__main__":
    main()
