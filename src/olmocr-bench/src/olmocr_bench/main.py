"""Command-line entry point: generate Harbor tasks for olmOCR-Bench.

Run from ``src/olmocr-bench/``:

    uv run olmocr-bench --output-dir ../../datasets/olmocr-bench
    uv run olmocr-bench --split parity --output-dir ../../datasets/olmocr-bench-parity
"""

import argparse
import logging
from pathlib import Path

from .adapter import SPLITS, OlmOCRBenchAdapter

# Default output dir: <repo>/datasets/olmocr-bench
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "datasets" / "olmocr-bench"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate Harbor tasks for olmOCR-Bench (one task per PDF)."
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
        help="Generate only the first N tasks (after --split and --task-ids)",
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
        help=(
            "Only generate these tasks. Accepts Harbor task IDs "
            "(arxiv-math-2503-04048-pg46) or upstream PDF paths "
            "(arxiv_math/2503.04048_pg46.pdf)"
        ),
    )
    parser.add_argument(
        "--split",
        choices=SPLITS,
        default="full",
        help="'full' generates every PDF; 'parity' generates the parity subset",
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=None,
        help=(
            "Local copy of the allenai/olmOCR-bench dataset (the directory that "
            "contains bench_data/). Downloaded from Hugging Face when omitted"
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    # The dataset download issues one request per PDF; keep that out of the log.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    adapter = OlmOCRBenchAdapter(
        args.output_dir,
        overwrite=args.overwrite,
        limit=args.limit,
        task_ids=args.task_ids,
        split=args.split,
        source_dir=args.source_dir,
    )
    try:
        adapter.run()
    except (OSError, RuntimeError, ValueError) as exc:
        # Missing or malformed benchmark data, failed download, unwritable output.
        raise SystemExit(f"error: {exc}") from exc


if __name__ == "__main__":
    main()
