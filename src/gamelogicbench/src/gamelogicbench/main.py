from __future__ import annotations

import argparse
from pathlib import Path
import subprocess

from .adapter import DEFAULT_RUNTIME_IMAGE, GameLogicBenchAdapter
from .sources import SourceCheckouts


def _select_ids(available: list[str], args: argparse.Namespace) -> list[str]:
    if args.split == "parity":
        if args.parity_task_file is None:
            raise ValueError("--split parity requires the approved --parity-task-file")
        selected = [
            line
            for raw_line in args.parity_task_file.read_text(encoding="utf-8").splitlines()
            if (line := raw_line.partition("#")[0].strip())
        ]
        if not selected:
            raise ValueError("Parity task file is empty")
        if len(selected) != len(set(selected)):
            raise ValueError("Parity task file contains duplicate IDs")
    else:
        if args.parity_task_file is not None:
            raise ValueError("--parity-task-file requires --split parity")
        selected = available

    unknown = set(selected) - set(available)
    if unknown:
        raise ValueError(f"Unknown upstream task IDs: {', '.join(sorted(unknown))}")
    if args.task_ids:
        requested = set(args.task_ids)
        unknown = requested - set(selected)
        if unknown:
            raise ValueError(f"Task IDs outside the selected split: {', '.join(sorted(unknown))}")
        selected = sorted(requested)
    else:
        selected = sorted(selected)
    if args.limit is not None:
        if args.limit < 1:
            raise ValueError("--limit must be positive")
        selected = selected[:args.limit]
    return selected


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Generate local Harbor tasks from GameLogicBench")
    parser.add_argument("--harness-dir", type=Path, help="Pinned local GameLogicBench checkout")
    parser.add_argument("--tasks-dir", type=Path, help="Pinned local GameLogicBench-Tasks checkout")
    parser.add_argument("--output-dir", type=Path, required=True, help="Private generated task directory")
    parser.add_argument("--limit", type=int, help="Generate at most this many tasks")
    parser.add_argument("--overwrite", action="store_true", help="Replace previously generated tasks")
    parser.add_argument("--task-ids", nargs="+", help="Select specific upstream task IDs")
    parser.add_argument("--split", choices=["full", "parity"], default="full")
    parser.add_argument("--parity-task-file", type=Path, help="Approved parity IDs, one per line")
    parser.add_argument("--runtime-image", default=DEFAULT_RUNTIME_IMAGE)
    parser.add_argument("--allow-host", action="append", default=[], help="Allow a model API hostname")
    parser.add_argument(
        "--reference", choices=["proper", "naive"], default="proper",
        help="Oracle overlay to use. Use naive only for local negative calibration.",
    )
    args = parser.parse_args(argv)
    if args.harness_dir is None or args.tasks_dir is None:
        parser.error("--harness-dir and --tasks-dir require the local pinned upstream checkouts")
    try:
        source = SourceCheckouts(args.harness_dir, args.tasks_dir)
        task_ids = _select_ids(source.task_ids, args)
        adapter = GameLogicBenchAdapter(
            source, args.output_dir,
            task_ids=task_ids,
            runtime_image=args.runtime_image,
            allowed_hosts=tuple(sorted(set(args.allow_host))),
            reference=args.reference,
            overwrite=args.overwrite,
        )
        generated, skipped, cells = adapter.run()
    except (OSError, KeyError, ValueError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))
    print(f"Generated {generated} tasks and {cells} judge cells; skipped {skipped} existing tasks.")


if __name__ == "__main__":
    main()
