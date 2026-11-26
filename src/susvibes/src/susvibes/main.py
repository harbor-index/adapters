"""CLI entry point: generate Harbor tasks for SusVibes.

Run from ``src/susvibes/``::

    uv run susvibes --output-dir ../../datasets/susvibes
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from susvibes.adapter import SusVibesAdapter
else:
    from .adapter import SusVibesAdapter


def _default_output_dir() -> Path:
    """<repo>/datasets/susvibes when run from a checkout of the adapters repo,
    otherwise datasets/susvibes under the current directory."""
    repo_root = Path(__file__).resolve().parents[4]
    if (repo_root / "src" / "susvibes" / "pyproject.toml").is_file():
        return repo_root / "datasets" / "susvibes"
    return Path.cwd() / "datasets" / "susvibes"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert SusVibes instances into Harbor task directories"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=_default_output_dir(),
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
        help="Only generate these task IDs (SusVibes instance ids)",
    )
    parser.add_argument(
        "--split",
        choices=["full", "parity"],
        default="full",
        help="'full' generates the 178 adapted instances; 'parity' the fixed 20-task subset",
    )
    parser.add_argument(
        "--susvibes-root",
        type=Path,
        default=None,
        help="Local checkout of LeiLiLab/susvibes at the pinned commit "
        "(default: download the pinned files from GitHub)",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Where downloaded upstream files are cached",
    )
    args = parser.parse_args()

    adapter = SusVibesAdapter(
        args.output_dir,
        overwrite=args.overwrite,
        limit=args.limit,
        task_ids=args.task_ids,
        split=args.split,
        susvibes_root=args.susvibes_root,
        cache_dir=args.cache_dir,
    )

    adapter.run()


if __name__ == "__main__":
    main()
