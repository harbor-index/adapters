from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .adapter import DEFAULT_CPUS, DEFAULT_MEMORY_MB, LoopsBenchAdapter, Resources

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

DEFAULT_OUTPUT_DIR = Path("datasets/loopsbench")

# Frozen from the 104 reviewed c47e13a tasks. The README describes the sampling rule.
PARITY_TASK_IDS = (
    "distributed-fs-chfs", "architecture-riscv-labs",
    "evosuite-test-generation", "boogie-dafny",
    "navidrome-seg02", "echarts-seg05",
    "numpy-dl-aics-style", "cocos2dx-physics2d-medium",
    "deep-learning-three-assignments", "ligra-graph-evolution",
)


def main() -> None:
    p = argparse.ArgumentParser(description="Generate Harbor tasks from LoopsBench")
    p.add_argument(
        "--tasks-dir",
        type=Path,
        required=True,
        help="Extracted LoopsBench task bundle (the tasks/ directory from "
        "huggingface.co/datasets/LoopsBench/LoopsBench)",
    )
    p.add_argument("--split", choices=("full", "parity"), default="full")
    p.add_argument("--output-dir", type=Path,
                   help="Defaults to datasets/loopsbench or datasets/loopsbench-parity")
    p.add_argument("--task-ids", nargs="+", default=None,
                   help="Upstream ids (task_foo) or Harbor ids (foo)")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--overwrite", action="store_true",
                   help="Replace complete tasks as well as retrying incomplete tasks")
    p.add_argument("--bundle", default="c47e13a",
                   help="Upstream bundle revision recorded in task metadata")
    p.add_argument("--cpus", type=int,
                   help=f"Override per-task CPU allocation (normally {DEFAULT_CPUS})")
    p.add_argument("--memory-mb", type=int,
                   help=f"Override per-task memory in MiB (normally {DEFAULT_MEMORY_MB})")
    p.add_argument("--storage-mb", type=int, default=Resources.storage_mb)
    args = p.parse_args()

    output_dir = args.output_dir or DEFAULT_OUTPUT_DIR
    task_ids = args.task_ids
    parity = args.split == "parity"
    if parity:
        if task_ids is not None or args.limit is not None:
            p.error("--split parity selects a fixed set and cannot be combined with --task-ids or --limit")
        output_dir = args.output_dir or Path("datasets/loopsbench-parity")
        task_ids = list(PARITY_TASK_IDS)

    adapter = LoopsBenchAdapter(
        tasks_dir=args.tasks_dir,
        output_dir=output_dir,
        bundle=args.bundle,
        task_ids=task_ids,
        limit=args.limit,
        overwrite=args.overwrite,
        resources=Resources(cpus=args.cpus, memory_mb=args.memory_mb, storage_mb=args.storage_mb),
    )
    try:
        written = adapter.run(exact_output=parity)
    except (OSError, ValueError) as exc:
        p.error(str(exc))
    if parity:
        logging.info("Parity output contains %d complete tasks in %s (%d generated)",
                     len(PARITY_TASK_IDS), output_dir, len(written))
    else:
        logging.info("Generated %d task(s) in %s", len(written), output_dir)


if __name__ == "__main__":
    main()
