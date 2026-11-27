"""CLI entry point for the LatchBio (benchmarks.bio) adapter.

Constructs the adapter defined in adapter.py and calls run() to generate tasks in
the Harbor format at the configured output directory.

    cd src/latchbio
    uv run latchbio --output-dir ../../datasets/latchbio
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .adapter import LatchBioAdapter
from .data import DATA_SOURCES, SPLITS, DataError
from .graders_release import GradersReleaseError
from .sources import BENCHMARKS_BY_KEY
from .upstream import UpstreamError

# Default output dir: <repo>/datasets/<adapter_id>
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "datasets" / "latchbio"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="latchbio",
        description="Generate Harbor tasks from LatchBio's benchmarks.bio evaluations.",
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
        help=(
            "Only generate these tasks. Accepts Harbor task ids "
            "(scbench__de01_pseudobulk_de), registry names "
            "(latchbio/scbench__de01_pseudobulk_de) or upstream eval ids "
            "(DE01_pseudobulk_de)"
        ),
    )
    parser.add_argument(
        "--benchmarks",
        nargs="+",
        default=None,
        choices=sorted(BENCHMARKS_BY_KEY),
        metavar="BENCHMARK",
        help=f"Only generate these benchmarks: {', '.join(sorted(BENCHMARKS_BY_KEY))}",
    )
    parser.add_argument(
        "--split",
        choices=SPLITS,
        default="full",
        help=(
            "'full' generates every adapted evaluation; 'small-data' only those "
            "whose input data totals at most 2 GiB (38 of 44), for hosts that "
            "cannot give a container 32 GB of memory"
        ),
    )
    parser.add_argument(
        "--data-source",
        choices=DATA_SOURCES,
        default="mirror",
        help=(
            "Where evaluation data comes from: 'mirror' downloads pinned files at "
            "image build time (no credentials), 'latch' downloads them now with "
            "the Latch SDK (needs `latch login`) and copies them into each task, "
            "'none' stages no data (oracle/verifier checks only)"
        ),
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help=(
            "Cache for data downloaded with --data-source latch "
            "(default: $LATCHBIO_DATA_DIR or ~/.cache/harbor-latchbio/data)"
        ),
    )
    parser.add_argument(
        "--upstream-root",
        type=Path,
        default=None,
        help=(
            "Directory containing existing clones of the upstream repositories "
            "(scbench/, spatialbench/, ...) at their pinned commits"
        ),
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Where upstream repositories are fetched (default: ~/.cache/harbor-latchbio/upstream)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List the tasks that would be generated and exit",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()

    try:
        adapter = LatchBioAdapter(
            args.output_dir,
            overwrite=args.overwrite,
            limit=args.limit,
            task_ids=args.task_ids,
            benchmarks=args.benchmarks,
            split=args.split,
            data_source=args.data_source,
            data_dir=args.data_dir,
            upstream_root=args.upstream_root,
            cache_dir=args.cache_dir,
        )
        if args.list:
            for item in adapter.select_evals():
                print(f"{item.task_name}\t{item.source.repo}/blob/{item.source.commit}/{item.path}")
            return
        adapter.run()
    except (DataError, GradersReleaseError, UpstreamError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
