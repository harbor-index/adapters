"""CLI entry point for the PG-LLM Harbor adapter."""

from __future__ import annotations

import argparse
from pathlib import Path

from .adapter import EVAL_SEEDS, EVAL_SIZES, NETWORK_MODES, PgLlmAdapter

def _default_output_dir() -> Path:
    resolved = Path(__file__).resolve()
    if len(resolved.parents) > 4 and (resolved.parents[4] / "scripts" / "validate_adapter.py").is_file():
        return resolved.parents[4] / "datasets" / "pg-llm"
    return Path.cwd() / "datasets" / "pg-llm"


DEFAULT_OUTPUT_DIR = _default_output_dir()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate Harbor tasks from the frozen PG-LLM (ProteinGym-LLM) evaluation bundle."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=(
            "Directory to write generated tasks "
            f"(default: <repo>/datasets/pg-llm in a checkout, or ./datasets/pg-llm otherwise; currently {DEFAULT_OUTPUT_DIR})."
        ),
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Optional local path to an already-extracted eval-data-v1.2 directory.",
    )
    parser.add_argument(
        "--size",
        type=int,
        default=50,
        choices=list(EVAL_SIZES),
        help=(
            "Nominal variant candidate set size per task (default: 50; "
            "note: NPC1_HUMAN_Erwood_2022_RPE1 has 63 candidates at nominal N=100)."
        ),
    )
    parser.add_argument(
        "--seeds",
        type=int,
        nargs="+",
        default=[1],
        choices=list(EVAL_SEEDS),
        help="Evaluation batch seeds to include (default: 1 -> 217 tasks; 1 2 3 -> 651 tasks).",
    )
    parser.add_argument(
        "--split",
        type=str,
        default="all",
        choices=["all", "parity"],
        help=(
            "Dataset split: 'all' (all 217 assays) or 'parity' "
            "(50 stratified assays: 10 per category -> 50 tasks per selected seed)."
        ),
    )
    parser.add_argument(
        "--network-mode",
        type=str,
        default="public",
        choices=list(NETWORK_MODES),
        help=(
            "Task [environment]/[agent]/[verifier] network_mode (default: 'public' "
            "for local Docker compatibility including macOS Docker Desktop; use "
            "'no-network' for strict egress isolation on Daytona, Modal, or Linux "
            "Docker with CONFIG_NFT_FIB_INET)."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Generate only the first N tasks.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help=(
            "Stage task replacements with rollback on failure and prune stale PG-LLM "
            "task directories outside the current selection (refuses to delete unrelated directories)."
        ),
    )
    parser.add_argument(
        "--task-ids",
        nargs="+",
        default=None,
        help="Only generate matching task IDs (sanitized Harbor task ID or upstream DMS_id).",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    adapter = PgLlmAdapter(
        args.output_dir,
        data_dir=args.data_dir,
        size=args.size,
        seeds=args.seeds,
        split=args.split,
        network_mode=args.network_mode,
        limit=args.limit,
        overwrite=args.overwrite,
        task_ids=args.task_ids,
    )
    adapter.run()


if __name__ == "__main__":
    main()
