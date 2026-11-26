"""
Main entry point for the template adapter. Do not modify any of the existing flags.
You can add any additional flags you need.

Constructs the Adapter class defined in adapter.py and calls run() to generate tasks
in the Harbor format at the configured output directory.
"""

import argparse
from pathlib import Path

from .adapter import OnlineMind2WebAdapter

# Default output dir: <repo>/datasets/<adapter_id>
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "datasets" / "online-mind2web"


def main() -> None:
    parser = argparse.ArgumentParser()
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
        help="Only generate these task IDs",
    )
    parser.add_argument(
        "--dataset-path",
        type=Path,
        default=None,
        help="Local Online_Mind2Web.json; default downloads it from the gated HF dataset "
        "osunlp/Online-Mind2Web (accept its terms and set HF_TOKEN or `hf auth login`)",
    )
    parser.add_argument(
        "--split",
        choices=["full", "parity"],
        default="full",
        help="'parity' = deterministic difficulty-stratified subset used for parity runs",
    )
    parser.add_argument(
        "--parity-size",
        type=int,
        default=100,
        help="Number of tasks in the parity split",
    )
    args = parser.parse_args()

    adapter = OnlineMind2WebAdapter(
        args.output_dir,
        overwrite=args.overwrite,
        limit=args.limit,
        task_ids=args.task_ids,
        dataset_path=args.dataset_path,
        split=args.split,
        parity_size=args.parity_size,
    )

    adapter.run()


if __name__ == "__main__":
    main()
