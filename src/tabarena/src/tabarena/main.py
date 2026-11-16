"""Canonical task-generation command; model execution is a separate step."""

import argparse
from pathlib import Path

from .adapter import TabArenaAdapter

DEFAULT_OUTPUT_DIR = Path("datasets") / "tabarena"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate TabArena tasks (official TabArena-Lite split by default)"
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--task-ids", nargs="+", default=None)
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Host-only data cache; never copied to an agent environment",
    )
    parser.add_argument(
        "--manifest-only",
        action="store_true",
        help="Write selected task manifest without downloading data or generating tasks",
    )
    args = parser.parse_args()
    try:
        TabArenaAdapter(**vars(args)).run()
    except (ValueError, FileExistsError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
