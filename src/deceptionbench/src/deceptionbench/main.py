"""Command-line task generation for DeceptionBench's single-turn conditions."""

from __future__ import annotations

import argparse
from pathlib import Path

from .adapter import DeceptionBenchAdapter
from .dataset import DEFAULT_CACHE_DIR, DOMAINS
from .protocol import CONDITIONS


def main() -> None:
    cwd = Path.cwd().resolve()
    root = next(
        (
            parent
            for parent in (cwd, *cwd.parents)
            if (parent / "docs/adapters.mdx").is_file()
        ),
        cwd,
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", type=Path, default=root / "datasets/deceptionbench"
    )
    parser.add_argument(
        "--data-dir", type=Path, help="Directory containing the five pinned JSON files"
    )
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--task-ids", nargs="+")
    parser.add_argument("--domains", nargs="+", choices=DOMAINS)
    parser.add_argument("--conditions", nargs="+", choices=CONDITIONS)
    parser.add_argument("--split", choices=("full", "parity"), default="full")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    try:
        paths = DeceptionBenchAdapter(**vars(args)).run()
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"Generated {len(paths)} tasks in {args.output_dir}")


if __name__ == "__main__":
    main()
