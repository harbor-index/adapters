"""
CLI entry point for the JAXBench adapter (`uv run jaxbench`).

Constructs JAXBenchAdapter (adapter.py) and calls run() to generate Harbor tasks
in the configured output directory.
"""

import argparse
from pathlib import Path

from .adapter import JAXBenchAdapter

# Default output dir: <repo>/datasets/<adapter_id>
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "datasets" / "jaxbench"


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
        help="Only generate these task IDs (e.g. level1-8p-gemm or 8p_GEMM)",
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=None,
        help="Local accelerator-agents checkout (default: clone the pinned upstream commit)",
    )
    parser.add_argument(
        "--levels",
        nargs="+",
        default=None,
        help="JAXBench levels to include (default: level1)",
    )
    parser.add_argument(
        "--split",
        choices=["full", "parity"],
        default="full",
        help="Generate all adapted tasks (default) or only the parity subset",
    )
    parser.add_argument(
        "--oracle",
        choices=["pallas", "baseline"],
        default="pallas",
        help="Oracle solution: upstream Pallas kernel where it passes the harness (default), "
        "or the XLA baseline for every task",
    )
    args = parser.parse_args()

    adapter = JAXBenchAdapter(
        args.output_dir,
        overwrite=args.overwrite,
        limit=args.limit,
        task_ids=args.task_ids,
        source_dir=args.source_dir,
        levels=args.levels,
        oracle=args.oracle,
        split=args.split,
    )

    adapter.run()


if __name__ == "__main__":
    main()
