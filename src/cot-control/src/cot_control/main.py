"""CLI entry point: generate Harbor tasks for CoT-Control.

Run from ``src/cot-control``::

    uv run cot-control --output-dir ../../datasets/cot-control
    uv run cot-control --split parity --output-dir ../../datasets/cot-control-parity
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .adapter import SPLITS, CoTControlAdapter
from .constants import (
    DATASETS_BY_KEY,
    MODES_BY_NAME,
    PARITY_ROWS_PER_DATASET,
    PARITY_SEED,
)

# Default output dir: <repo>/datasets/cot-control
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "datasets" / "cot-control"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cot-control",
        description="Generate Harbor tasks for the CoT-Control QA benchmark.",
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
        help="Generate only the first N tasks (row-major order: the first 27 tasks "
        "cover every dataset x instruction-type pair)",
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
        help="Only generate these task IDs, e.g. gpqa-0007-word-suppression",
    )
    parser.add_argument(
        "--split",
        choices=SPLITS,
        default="full",
        help="'full' generates every instance; 'parity' generates the fixed parity subset",
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=None,
        help="Local checkout of the upstream CoTControl repository at the pinned commit "
        "(default: download the three dataset files from GitHub and cache them)",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache directory for downloaded dataset files",
    )
    parser.add_argument(
        "--datasets",
        nargs="+",
        choices=sorted(DATASETS_BY_KEY),
        default=None,
        help="Restrict generation to these source datasets",
    )
    parser.add_argument(
        "--modes",
        nargs="+",
        choices=sorted(MODES_BY_NAME),
        default=None,
        help="Restrict generation to these instruction types",
    )
    parser.add_argument(
        "--prompt-model-name",
        default="",
        help="Model name passed to the upstream prompt builder. Upstream words the "
        "instruction as 'analysis channel' when the name contains 'gpt-oss' and as "
        "'reasoning stage' otherwise (default)",
    )
    parser.add_argument(
        "--parity-rows-per-dataset",
        type=int,
        default=PARITY_ROWS_PER_DATASET,
        help="Questions per dataset in the parity split",
    )
    parser.add_argument(
        "--parity-seed",
        type=int,
        default=PARITY_SEED,
        help="Seed of the parity split",
    )
    parser.add_argument(
        "--export-upstream-subset",
        type=Path,
        default=None,
        metavar="DIR",
        help="Also write upstream-format CSV files containing only the selected "
        "questions, for running the same subset with upstream's run_cceval.py --data",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    adapter = CoTControlAdapter(
        args.output_dir,
        overwrite=args.overwrite,
        limit=args.limit,
        task_ids=args.task_ids,
        split=args.split,
        source_dir=args.source_dir,
        cache_dir=args.cache_dir,
        datasets=args.datasets,
        modes=args.modes,
        prompt_model_name=args.prompt_model_name,
        parity_rows_per_dataset=args.parity_rows_per_dataset,
        parity_seed=args.parity_seed,
    )
    adapter.run()
    if args.export_upstream_subset is not None:
        adapter.export_upstream_subset(args.export_upstream_subset)


if __name__ == "__main__":
    main()
