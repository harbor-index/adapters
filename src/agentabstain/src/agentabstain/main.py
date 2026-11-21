"""Command-line entrypoint for the AgentAbstain Harbor adapter."""

import argparse
import os
from pathlib import Path

from .adapter import AgentAbstainAdapter

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory to write generated tasks",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Generate only the first N source rows",
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
        help="Only generate rows matching a category-qualified source, pair, or neutral task ID",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("AGENTABSTAIN_DATA", "data")),
        help="Path to the downloaded AgentAbstain dataset",
    )
    parser.add_argument(
        "--split",
        choices=("full", "smoke", "parity"),
        default="full",
        help="Use the full corpus, one smoke pair, or the 42-environment parity sample",
    )
    args = parser.parse_args()

    adapter = AgentAbstainAdapter(
        args.output_dir,
        overwrite=args.overwrite,
        limit=args.limit,
        task_ids=args.task_ids,
        data_dir=args.data_dir,
        split=args.split,
    )

    adapter.run()


if __name__ == "__main__":
    main()
