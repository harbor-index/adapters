"""Generate RLE-Bench tasks from an immutable upstream revision."""
import argparse
import logging
from pathlib import Path
import subprocess

from .adapter import RLEBenchAdapter, UPSTREAM_COMMIT, select_tasks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("cpu", "gpu"), default="cpu")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--source-dir", type=Path,
                        help=f"Clean local checkout at {UPSTREAM_COMMIT}; otherwise fetch the pinned revision")
    parser.add_argument("--limit", type=int, help="Generate the first N selected tasks")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing generated tasks")
    parser.add_argument("--task-ids", nargs="+", help="Task IDs or native selectors in the selected split")
    parser.add_argument("--list", action="store_true", help="List available task IDs in the selected split")
    args = parser.parse_args()
    if args.list:
        print("\n".join(task.task_id for task in select_tasks(None, None, split=args.split)))
        return
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        output = args.output_dir or Path(f"datasets/rle-bench-{args.split}")
        RLEBenchAdapter(output, limit=args.limit, overwrite=args.overwrite,
                        task_ids=args.task_ids, source_dir=args.source_dir, split=args.split).run()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"error: {error}\n")


if __name__ == "__main__":
    main()
