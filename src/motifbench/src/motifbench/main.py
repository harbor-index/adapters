"""CLI entry point for generating MotifBench Harbor tasks."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .adapter import (
    DEFAULT_HF_REPO,
    DEFAULT_HF_REVISION,
    MotifBenchAdapter,
)

# main.py lives at src/motifbench/src/motifbench/main.py.
# parents[4] is the harbor-index/adapters repository root.
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "datasets" / "motifbench"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate Harbor tasks for MotifBench (Critic's Eye)."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory to write generated task directories.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help=(
            "Optional local directory containing macbeth_full.txt, macbeth_full.pdf, "
            "macbeth_clean.jsonl, and motifs_clean.json. If omitted, downloads from HuggingFace."
        ),
    )
    parser.add_argument(
        "--hf-repo",
        type=str,
        default=DEFAULT_HF_REPO,
        help="HuggingFace dataset repo ID.",
    )
    parser.add_argument(
        "--hf-revision",
        type=str,
        default=DEFAULT_HF_REVISION,
        help="Pinned HuggingFace dataset revision.",
    )
    parser.add_argument(
        "--granularity",
        choices=("scene-group", "act", "query"),
        default="scene-group",
        help="Task packaging granularity: scene-group (12 tasks, default), act (5 tasks), or query (270 tasks).",
    )
    parser.add_argument(
        "--split",
        choices=("full", "parity"),
        default="full",
        help="Dataset split to generate (parity selects all 12 scene-group tasks or 50 stratified query tasks).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Generate only the first N tasks.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing generated task directories.",
    )
    parser.add_argument(
        "--task-ids",
        nargs="+",
        default=None,
        help="Only generate tasks with these task IDs.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    adapter = MotifBenchAdapter(
        args.output_dir,
        data_dir=args.data_dir,
        hf_repo=args.hf_repo,
        hf_revision=args.hf_revision,
        granularity=args.granularity,
        split=args.split,
        limit=args.limit,
        overwrite=args.overwrite,
        task_ids=args.task_ids,
    )
    adapter.run()


if __name__ == "__main__":
    main()
