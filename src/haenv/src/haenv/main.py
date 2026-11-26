"""CLI entry point for the HAEnv Harbor adapter."""

from __future__ import annotations

import argparse
from pathlib import Path

from .adapter import DEFAULT_PACK_SIZE, HealthAgentEnvironmentAdapter

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "datasets" / "haenv"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Harbor tasks for HAEnv (M2 Diagnosis Track)")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory to write generated Harbor task directories",
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
        help="Overwrite existing generated task directories",
    )
    parser.add_argument(
        "--task-ids",
        nargs="+",
        default=None,
        help="Only generate tasks matching these case IDs or Harbor task IDs",
    )
    parser.add_argument(
        "--split",
        choices=["diagnosis", "parity"],
        default="diagnosis",
        help="HAEnv split to adapt (diagnosis = full N=100 M2 pack; parity = parity subset)",
    )
    parser.add_argument(
        "--n",
        type=int,
        default=DEFAULT_PACK_SIZE,
        help=f"Number of audited M2 items to build when --pack-dir is not supplied (default: {DEFAULT_PACK_SIZE})",
    )
    parser.add_argument(
        "--seed",
        default=None,
        help="Optional pack seed passed to upstream make_pack.py (defaults to public seed)",
    )
    parser.add_argument(
        "--lang",
        choices=["en", "zh"],
        default="en",
        help="Language for records and prompts (default: en)",
    )
    parser.add_argument(
        "--pack-dir",
        type=Path,
        default=None,
        help="Path to an existing audited make_pack output directory or batch directory",
    )
    parser.add_argument(
        "--upstream-repo",
        type=Path,
        default=None,
        help="Optional local clone of thetahealth/mirobody-env to avoid re-cloning",
    )
    args = parser.parse_args()

    adapter = HealthAgentEnvironmentAdapter(
        output_dir=args.output_dir,
        limit=args.limit,
        overwrite=args.overwrite,
        task_ids=args.task_ids,
        split=args.split,
        n_items=args.n,
        seed=args.seed,
        lang=args.lang,
        pack_dir=args.pack_dir,
        upstream_repo=args.upstream_repo,
    )
    adapter.run()


if __name__ == "__main__":
    main()
