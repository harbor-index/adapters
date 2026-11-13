"""Generate DeepPlanning Shopping levels 1–2 Harbor tasks."""

import argparse
import subprocess
from pathlib import Path

from .adapter import DeepPlanningShoppingAdapter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", type=Path, default=Path("datasets/deepplanning-shopping")
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        help="Qwen-Agent Git checkout containing the pinned commit",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="Directory containing pinned database_level1/2.tar.gz",
    )
    parser.add_argument("--task-ids", nargs="+")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--split", choices=("full", "parity"), default="full")
    parser.add_argument("--environment", choices=("smol", "docker"), default="smol")
    args = parser.parse_args()
    try:
        paths = DeepPlanningShoppingAdapter(**vars(args)).run()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"deepplanning-shopping: {error}\n")
    print(
        f"Generated {len(paths)} tasks in {args.output_dir.resolve()} ({args.environment})"
    )


if __name__ == "__main__":
    main()
