"""Generate Harbor tasks from the TimeMachine-bench Verified split.

uv run timemachine-bench --output-dir datasets/timemachine-bench
uv run timemachine-bench --output-dir datasets/timemachine-bench-parity --split parity
"""

import argparse
from pathlib import Path

from .adapter import TimeMachineBenchAdapter

# Default output dir: <repo>/datasets/<adapter_id>
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "datasets" / "timemachine-bench"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
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
        help="Only generate these task IDs (e.g. brmc__shortbus, or the upstream name brmc/shortbus)",
    )
    parser.add_argument(
        "--split",
        choices=["verified", "parity"],
        default="verified",
        help="'verified': all 100 records of the Verified split (default); "
        "'parity': the 10-task parity subset",
    )
    parser.add_argument(
        "--jsonl",
        type=Path,
        default=None,
        help="Local copy of timemachine-bench-verified.jsonl. By default the file is "
        "downloaded from the pinned upstream commit and its sha256 is checked.",
    )
    args = parser.parse_args()

    adapter = TimeMachineBenchAdapter(
        args.output_dir,
        overwrite=args.overwrite,
        limit=args.limit,
        task_ids=args.task_ids,
        split=args.split,
        jsonl_path=args.jsonl,
    )

    adapter.run()


if __name__ == "__main__":
    main()
