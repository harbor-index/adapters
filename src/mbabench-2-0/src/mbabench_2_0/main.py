"""CLI entry point for the MBABench 2.0 adapter.

Usage (from src/mbabench-2-0/):
    uv run mbabench-2-0 --output-dir ../../datasets/mbabench-2-0
    uv run mbabench-2-0 --output-dir ../../datasets/mbabench-2-0 --split parity
    uv run mbabench-2-0 --output-dir /tmp/mbabench --task-ids 1 2 --overwrite
    uv run mbabench-2-0 --dataset-dir /path/to/MBABench-2.0/data --output-dir ...
"""

import argparse
import logging
from pathlib import Path

from .adapter import SPLITS, MBABench20Adapter

# Default output dir: <repo>/datasets/<adapter_id>
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "datasets" / "mbabench-2-0"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
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
        help="Generate only the first N tasks (by upstream task id)",
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
        help="Only generate these upstream task IDs (integers 1-101)",
    )
    parser.add_argument(
        "--split",
        choices=SPLITS,
        default="full",
        help="'full' = all 101 tasks; 'parity' = the 30-task parity subset",
    )
    parser.add_argument(
        "--dataset-dir",
        type=Path,
        default=None,
        help=(
            "Use an existing local copy of the dataset (the folder holding "
            "MANIFEST.json and tasks/) instead of downloading the pinned "
            "Hugging Face revision"
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    # huggingface_hub logs every file request through httpx at INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    adapter = MBABench20Adapter(
        args.output_dir,
        overwrite=args.overwrite,
        limit=args.limit,
        task_ids=args.task_ids,
        split=args.split,
        dataset_dir=args.dataset_dir,
    )
    adapter.run()


if __name__ == "__main__":
    main()
