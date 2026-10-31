"""CLI entry point: generate DeepResearch-Bench-II tasks in Harbor format."""

import argparse
from pathlib import Path

from .adapter import DeepResearchBenchAdapter

# Default output dir: <repo>/datasets/<adapter_id>
DEFAULT_OUTPUT_DIR = (
    Path(__file__).resolve().parents[4] / "datasets" / "deepresearch-bench"
)


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
        "--split",
        choices=["full", "parity"],
        default="full",
        help="Dataset split to generate: 'full' (132 tasks) or 'parity' (33-task parity subset)",
    )
    args = parser.parse_args()

    adapter = DeepResearchBenchAdapter(
        args.output_dir,
        overwrite=args.overwrite,
        limit=args.limit,
        task_ids=args.task_ids,
        split=args.split,
    )

    adapter.run()


if __name__ == "__main__":
    main()
