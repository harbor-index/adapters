import argparse
from pathlib import Path

from .adapter import DEFAULT_CACHE_DIR, DEFAULT_PROFILE, UPSTREAM_COMMIT, AgentSWEAdapter

# Default output dir: <repo>/datasets/<adapter_id>
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "datasets" / "agentswe"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Harbor tasks from AgentSWE Creation tasks.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Directory to write generated tasks")
    parser.add_argument("--limit", type=int, default=None, help="Generate only the first N tasks")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing tasks")
    parser.add_argument("--task-ids", nargs="+", default=None,
                        help="Only generate these task IDs (upstream id or short name, e.g. repo)")
    parser.add_argument("--split", choices=("full", "lite", "parity"), default="full",
                        help="full: every adapted task; lite/parity: the AgentSWE-Lite tasks")
    parser.add_argument("--profile", default=DEFAULT_PROFILE,
                        help=f"Upstream configuration profile whose overlays are applied (default: {DEFAULT_PROFILE})")
    parser.add_argument("--upstream-dir", type=Path, default=None,
                        help=f"Existing AgentSWE checkout at commit {UPSTREAM_COMMIT[:12]} (default: clone into --cache-dir)")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR,
                        help="Where the pinned AgentSWE checkout is cloned (default: %(default)s)")
    args = parser.parse_args()
    AgentSWEAdapter(
        args.output_dir,
        overwrite=args.overwrite,
        limit=args.limit,
        task_ids=args.task_ids,
        split=args.split,
        profile=args.profile,
        upstream_dir=args.upstream_dir,
        cache_dir=args.cache_dir,
    ).run()


if __name__ == "__main__":
    main()
