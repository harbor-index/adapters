"""CLI entry point: ``uv run phybench --output-dir <dir>``."""

import argparse
import logging
import sys
from pathlib import Path

from .adapter import PHYBenchAdapter

# Default output dir: <repo>/datasets/phybench
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "datasets" / "phybench"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description="Generate Harbor tasks for PHYBench")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory to write generated tasks (default: datasets/phybench)",
    )
    parser.add_argument("--limit", type=int, default=None, help="Generate only the first N tasks")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing tasks")
    parser.add_argument(
        "--task-ids",
        nargs="+",
        default=None,
        help="Only generate these task IDs (e.g. 495 or phybench-495)",
    )
    parser.add_argument(
        "--data-file",
        type=Path,
        default=None,
        help="Local PHYBench-fullques_v1.json (default: download the pinned revision)",
    )
    parser.add_argument(
        "--eed-dir",
        type=Path,
        default=None,
        help="Local phybench-official/phybench checkout (default: download the pinned EED files)",
    )
    args = parser.parse_args()

    adapter = PHYBenchAdapter(
        args.output_dir,
        overwrite=args.overwrite,
        limit=args.limit,
        task_ids=args.task_ids,
        data_file=args.data_file,
        eed_dir=args.eed_dir,
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
