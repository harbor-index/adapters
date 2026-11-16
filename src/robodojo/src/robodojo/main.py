"""Command-line entry point for the RoboDojo adapter."""

import argparse
from pathlib import Path

from .adapter import DEFAULT_IMAGE, RoboDojoAdapter


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--assets-dir", type=Path, required=True,
                        help="Existing Assets directory on the Linux Docker host")
    parser.add_argument("--image", default=DEFAULT_IMAGE,
                        help="Pinned simulator image built by scripts/prepare_image.sh")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--task-ids", nargs="+", help="Canonical upstream task identifiers")
    args = parser.parse_args()
    try:
        tasks = RoboDojoAdapter(**vars(args)).run()
    except (ValueError, FileExistsError) as exc:
        parser.error(str(exc))
    print(f"Generated {len(tasks)} RoboDojo tasks in {args.output_dir}")


if __name__ == "__main__":
    main()
