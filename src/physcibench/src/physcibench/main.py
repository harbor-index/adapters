"""Command-line entry point for the PhySciBench dataset generator."""

import argparse
from pathlib import Path

from .adapter import PhySciBenchAdapter

PROJECT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = PROJECT_DIR / "datasets" / "physcibench"
DEFAULT_UPSTREAM_DIR = PROJECT_DIR.parents[2] / "physci-deepresearch"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--upstream-dir", type=Path, default=DEFAULT_UPSTREAM_DIR,
                        help="PhySciBench repository checkout containing eval/")
    parser.add_argument("--data-dir", type=Path,
                        help="Directory containing physcibench.json and files/")
    parser.add_argument("--limit", type=int, help="Generate at most N selected tasks")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing selected tasks")
    parser.add_argument("--task-ids", nargs="+", help="Generate only these upstream IDs")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    adapter = PhySciBenchAdapter(
        args.output_dir,
        args.upstream_dir,
        args.data_dir or args.upstream_dir / "PhySciBench",
        overwrite=args.overwrite,
        limit=args.limit,
        task_ids=args.task_ids,
    )
    try:
        adapter.run()
    except (OSError, ValueError) as exc:
        parser.exit(1, f"physcibench: {exc}\n")


if __name__ == "__main__":
    main()
