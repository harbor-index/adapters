"""Generate Harbor task directories from verified, pinned GameDevBench sources."""

import argparse
from pathlib import Path

from .adapter import GameDevBenchAdapter

# Default output dir: <repo>/datasets/<adapter_id>
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "datasets" / "gamedevbench"


def main() -> None:
    parser = argparse.ArgumentParser()
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
        help="Only generate these task IDs",
    )
    parser.add_argument("--source-dir", type=Path, required=True,
                        help="Materialized official source at the pinned revision")
    parser.add_argument("--runtime-image", help="Optional immutable prebuilt shared runtime")
    parser.add_argument("--use-mcp", action="store_true")
    parser.add_argument("--use-runtime-video", action="store_true")
    parser.add_argument("--workers", type=int, default=4, help="Local task-packaging threads; no LLM calls")
    parser.add_argument("--oracle-profile", choices=["original", "formal-verifier-v1"],
                        default="formal-verifier-v1", help="Reference-solution compatibility only; formal tests stay original")
    args = parser.parse_args()

    adapter = GameDevBenchAdapter(
        args.output_dir,
        overwrite=args.overwrite,
        limit=args.limit,
        task_ids=args.task_ids,
        source_dir=args.source_dir,
        runtime_image=args.runtime_image,
        use_mcp=args.use_mcp,
        use_runtime_video=args.use_runtime_video,
        workers=args.workers,
        oracle_profile=args.oracle_profile,
    )

    report = adapter.run()
    import json
    print(json.dumps({"generated": report["count"], "output_dir": str(args.output_dir)}))


if __name__ == "__main__":
    main()
