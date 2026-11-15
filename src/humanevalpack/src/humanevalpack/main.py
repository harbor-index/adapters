"""Command line entry point: generate HumanEvalPack tasks in the Harbor format.

Constructs the adapter defined in adapter.py and calls run() to write the task
directories into the configured output directory.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import upstream
from .adapter import (
    DEFAULT_SUBSETS,
    PARITY_TASKS_PER_STRATUM,
    SUBSETS,
    DatasetError,
    HumanEvalPackAdapter,
)

# <repository root>/datasets/humanevalpack, which is where run_humanevalpack.yaml looks.
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "datasets" / "humanevalpack"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="humanevalpack",
        description="Generate Harbor tasks for HumanEvalPack (HumanEvalFix and HumanEvalSynthesize, six languages).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory to write generated tasks (default: datasets/humanevalpack at the repository root)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Generate only the first N tasks of the selection (after --split and --task-ids)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Regenerate tasks whose directory already exists (they are skipped by default)",
    )
    parser.add_argument(
        "--task-ids",
        nargs="+",
        default=None,
        metavar="TASK_ID",
        help="Only generate these task IDs, e.g. fixtests-python-0 synthesize-rust-163",
    )
    parser.add_argument(
        "--split",
        choices=("full", "parity"),
        default="full",
        help=(
            "full: every problem; parity: a fixed, seeded subset with "
            f"{PARITY_TASKS_PER_STRATUM} problems per subset and language (default: full)"
        ),
    )
    parser.add_argument(
        "--subsets",
        nargs="+",
        choices=tuple(SUBSETS),
        default=None,
        help=(
            f"Upstream task families to generate (default: {' '.join(DEFAULT_SUBSETS)}; "
            "fixdocs is the docstring variant of HumanEvalFix from the paper's appendix)"
        ),
    )
    parser.add_argument(
        "--languages",
        nargs="+",
        choices=upstream.LANGUAGES,
        default=None,
        help="Languages (dataset configs) to generate (default: all six)",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Hugging Face cache directory for the dataset download (default: the Hugging Face default)",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    # huggingface_hub's HTTP client logs every request at INFO; keep that out of the log.
    for name in ("httpx", "httpx2"):
        logging.getLogger(name).setLevel(logging.WARNING)

    try:
        adapter = HumanEvalPackAdapter(
            args.output_dir,
            overwrite=args.overwrite,
            limit=args.limit,
            task_ids=args.task_ids,
            subsets=args.subsets,
            languages=args.languages,
            split=args.split,
            cache_dir=args.cache_dir,
        )
        adapter.run()
    except (ValueError, DatasetError, OSError) as error:
        sys.exit(f"error: {error}")


if __name__ == "__main__":
    main()
