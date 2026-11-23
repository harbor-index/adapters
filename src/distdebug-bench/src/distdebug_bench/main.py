"""CLI entry point: generate DistDebug-Bench tasks in Harbor format.

    uv run distdebug-bench --output-dir ../../datasets/distdebug-bench            # Lite (34 bugs)
    uv run distdebug-bench --split parity --output-dir ../../datasets/distdebug-bench-parity
    uv run distdebug-bench --task-ids zookeeper-2845 --overwrite
"""

import argparse
import logging
from pathlib import Path

from .adapter import DEFAULT_ALLOWED_HOSTS, DistDebugBenchAdapter

# Default output dir: <repo>/datasets/distdebug-bench
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "datasets" / "distdebug-bench"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Harbor tasks from DistDebug-Bench.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Directory to write generated tasks")
    parser.add_argument("--limit", type=int, default=None, help="Generate only the first N tasks")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing tasks")
    parser.add_argument("--task-ids", nargs="+", default=None, help="Only generate these task IDs (e.g. zookeeper-2845)")
    parser.add_argument(
        "--split",
        choices=("lite", "full", "parity"),
        default="lite",
        help="lite: the official 34-bug DistDebug-Bench Lite (default); full: all 120 bugs (not yet validated); "
        "parity: the subset used for parity experiments",
    )
    parser.add_argument("--source-dir", type=Path, default=None, help="Existing checkout of OrderLab/distdebug-bench (default: clone the pinned commit)")
    parser.add_argument("--cache-dir", type=Path, default=None, help="Where to clone the benchmark (default: ~/.cache/harbor-distdebug-bench)")
    parser.add_argument(
        "--agent-network",
        choices=("allowlist", "public", "no-network"),
        default="allowlist",
        help="Network policy for the agent phase (default: allowlist of LLM API hosts). "
        "Use 'public' only on environments without allowlist support; see README.",
    )
    parser.add_argument(
        "--allowed-hosts",
        nargs="+",
        default=None,
        help=f"Hosts reachable in the agent phase with --agent-network allowlist (default: {' '.join(DEFAULT_ALLOWED_HOSTS)})",
    )
    parser.add_argument("--agent-timeout-sec", type=float, default=3600.0, help="Agent timeout per task (default: 3600, as upstream CLI harnesses)")
    parser.add_argument("--allow-unpinned-source", action="store_true", help="Generate even if the upstream scorer differs from the pinned commit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    adapter = DistDebugBenchAdapter(
        args.output_dir,
        overwrite=args.overwrite,
        limit=args.limit,
        task_ids=args.task_ids,
        split=args.split,
        source_dir=args.source_dir,
        cache_dir=args.cache_dir,
        agent_network=args.agent_network,
        allowed_hosts=args.allowed_hosts,
        agent_timeout_sec=args.agent_timeout_sec,
        allow_unpinned_source=args.allow_unpinned_source,
    )
    written = adapter.run()
    print(f"Generated {len(written)} task(s) in {args.output_dir}")


if __name__ == "__main__":
    main()
