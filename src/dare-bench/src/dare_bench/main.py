"""Generate DARE-Bench tasks in Harbor format.

Example:
    uv run dare-bench --source-dir /path/to/dare-bench --output-dir datasets/dare-bench
"""

import argparse
from pathlib import Path

from .adapter import DAREBenchAdapter

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "datasets" / "dare-bench"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Directory to write generated tasks")
    parser.add_argument("--limit", type=int, default=None, help="Generate only the first N tasks (after sorting by id)")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing tasks")
    parser.add_argument("--task-ids", nargs="+", default=None, help="Only generate these task ids, e.g. <file_path>-v1")
    parser.add_argument(
        "--source-dir",
        type=Path,
        required=True,
        help="Checkout of https://github.com/Snowflake-Labs/dare-bench (needs data/eval, scripts, LICENSE)",
    )
    parser.add_argument("--split", choices=["full", "parity"], default="full", help="full = 324 tasks; parity = fixed 30")
    parser.add_argument("--variant", choices=["all", "v1", "v2"], default="all")
    args = parser.parse_args()

    DAREBenchAdapter(
        args.output_dir,
        overwrite=args.overwrite,
        limit=args.limit,
        task_ids=args.task_ids,
        source_dir=args.source_dir,
        split=args.split,
        variant=args.variant,
    ).run()


if __name__ == "__main__":
    main()
