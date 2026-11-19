"""Command-line entry point for SHADE-Arena task generation."""

import argparse
from pathlib import Path

from .adapter import ShadeArenaAdapter

# Default output dir: <repo>/datasets/<adapter_id>
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "datasets" / "shade-arena"


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
        "--source",
        type=Path,
        help="Local SHADE-Arena Git checkout containing the pinned revision (otherwise fetch it)",
    )
    args = parser.parse_args()

    adapter = ShadeArenaAdapter(
        args.output_dir,
        overwrite=args.overwrite,
        limit=args.limit,
        task_ids=args.task_ids,
        source=args.source,
    )

    try:
        adapter.run()
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
