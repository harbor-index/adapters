"""Command-line entry point: `uv run workspacebench --output-dir <path>`."""

import argparse
import logging
from pathlib import Path

from .adapter import WorkspaceBenchAdapter

# Default output dir: <repo>/datasets/<adapter_id>
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "datasets" / "workspacebench"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Directory to write generated tasks")
    parser.add_argument(
        "--split",
        choices=["full", "parity"],
        default="full",
        help="full: all tasks except EXCLUDED_TASKS (723). parity: the 50 English tasks parity ran on (see PARITY_IDS)",
    )
    parser.add_argument(
        "--languages",
        nargs="+",
        choices=["en", "cn"],
        default=["en", "cn"],
        help="Task languages to generate (default: both)",
    )
    parser.add_argument("--limit", type=int, default=None, help="Generate only the first N tasks")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing tasks")
    parser.add_argument("--task-ids", nargs="+", default=None, help="Only generate these tasks (names like en-033)")
    parser.add_argument(
        "--oracle-dir",
        type=Path,
        default=None,
        help="Directory of reference outputs, one folder per task name (e.g. <dir>/en-033/report.docx)",
    )
    parser.add_argument(
        "--tasks-root",
        type=Path,
        default=None,
        help="Use a local copy of the Workspace-Bench task repo instead of downloading it",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    WorkspaceBenchAdapter(
        args.output_dir,
        languages=args.languages,
        split=args.split,
        limit=args.limit,
        overwrite=args.overwrite,
        task_ids=args.task_ids,
        oracle_dir=args.oracle_dir,
        tasks_root=args.tasks_root,
    ).run()


if __name__ == "__main__":
    main()
