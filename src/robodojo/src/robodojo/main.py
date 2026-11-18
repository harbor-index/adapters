"""Command-line entry point for the RoboDojo adapter."""

import argparse
from pathlib import Path

from .adapter import DEFAULT_IMAGE, RoboDojoAdapter


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--assets-dir", type=Path,
                        help="Existing Assets directory on the Linux Docker host; not needed with --mock")
    parser.add_argument("--image", default=DEFAULT_IMAGE,
                        help="Pinned simulator image built by scripts/prepare_image.sh")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--task-ids", nargs="+", help="Canonical upstream task identifiers")
    parser.add_argument("--mock", action="store_true",
                        help="Generate the CPU mock task instead; it is not a benchmark task")
    parser.add_argument("--mock-episodes", type=int, default=2, help="Episodes of the mock task")
    args = parser.parse_args()
    if not args.mock and args.assets_dir is None:
        parser.error("--assets-dir is required unless --mock is given")
    try:
        tasks = RoboDojoAdapter(**vars(args)).run()
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    kind = "mock task (not a benchmark task)" if args.mock else "RoboDojo tasks"
    print(f"Generated {len(tasks)} {kind} in {args.output_dir}")


if __name__ == "__main__":
    main()
