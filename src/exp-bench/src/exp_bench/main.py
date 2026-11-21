"""CLI entry point for the EXP-Bench adapter.

Generates Harbor task directories from the EXP-Bench task bundle:

    uv run exp-bench --output-dir ../../datasets/exp-bench
    uv run exp-bench --split parity --output-dir ../../datasets/exp-bench-parity
"""

import argparse
from pathlib import Path

from .adapter import EXPBenchAdapter

# Default output dir: <repo>/datasets/<adapter_id>
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "datasets" / "exp-bench"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Harbor tasks for EXP-Bench")
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
        help="Only generate these task IDs (e.g. neurips2024-97649-0)",
    )
    parser.add_argument(
        "--split",
        choices=["full", "parity"],
        default="full",
        help="'parity' generates only the parity subset",
    )
    parser.add_argument(
        "--data-file",
        type=Path,
        default=None,
        help="Path to the EXP-Bench task bundle (.json or .json.gz; defaults to the packaged bundle)",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache for ground-truth source files downloaded from the pinned commits",
    )
    parser.add_argument(
        "--agent-timeout-hours",
        type=float,
        default=0.5,
        help="Per-task agent time limit; also used in the output file name, as upstream",
    )
    parser.add_argument("--cpus", type=int, default=4)
    parser.add_argument("--memory-mb", type=int, default=16384)
    parser.add_argument("--storage-mb", type=int, default=51200)
    parser.add_argument(
        "--gpus",
        type=int,
        default=0,
        help="GPUs per task (upstream ran agents with 4x A40; many tasks need a GPU)",
    )
    args = parser.parse_args()

    adapter = EXPBenchAdapter(
        args.output_dir,
        overwrite=args.overwrite,
        limit=args.limit,
        task_ids=args.task_ids,
        data_file=args.data_file,
        split=args.split,
        agent_timeout_hours=args.agent_timeout_hours,
        cpus=args.cpus,
        memory_mb=args.memory_mb,
        storage_mb=args.storage_mb,
        gpus=args.gpus,
        cache_dir=args.cache_dir,
    )
    adapter.run()


if __name__ == "__main__":
    main()
