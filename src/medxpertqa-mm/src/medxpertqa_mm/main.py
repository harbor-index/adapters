"""
CLI entry point for the MedXpertQA-MM adapter.

Run from src/medxpertqa-mm/:

    uv run medxpertqa-mm --output-dir ../../datasets/medxpertqa-mm
    uv run medxpertqa-mm --output-dir ../../datasets/medxpertqa-mm-parity --split parity
"""

import argparse
import logging
from pathlib import Path

from .adapter import MedXpertQAMMAdapter

# Default output dir: <repo>/datasets/<adapter_id>
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "datasets" / "medxpertqa-mm"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate Harbor tasks from the MedXpertQA-MM benchmark."
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
        help="Only generate these task IDs (e.g. mm-26 or MM-26)",
    )
    parser.add_argument(
        "--split",
        choices=["full", "parity"],
        default="full",
        help="full: all 2,000 MM test questions; parity: stratified 10%% subset",
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=None,
        help="Local dataset copy (MM/test.jsonl plus images/ or images.zip). "
        "Defaults to downloading TsinghuaC3I/MedXpertQA from HuggingFace.",
    )
    parser.add_argument(
        "--revision",
        type=str,
        default=None,
        help="HuggingFace dataset revision (commit hash, tag or branch). "
        "Defaults to the commit pinned in adapter.py.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    adapter = MedXpertQAMMAdapter(
        args.output_dir,
        overwrite=args.overwrite,
        limit=args.limit,
        task_ids=args.task_ids,
        split=args.split,
        source_dir=args.source_dir,
        revision=args.revision,
    )

    adapter.run()


if __name__ == "__main__":
    main()
