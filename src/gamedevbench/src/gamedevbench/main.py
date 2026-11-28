"""Generate Harbor task directories from verified, pinned GameDevBench sources."""

import argparse
import re
from pathlib import Path

from .adapter import GameDevBenchAdapter

# Default output dir: <repo>/datasets/<adapter_id>
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "datasets" / "gamedevbench"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
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
    parser.add_argument("--split", choices=("full", "parity"), default="full",
                        help="Generate the full benchmark or the fixed 30-task parity sample")
    args = parser.parse_args()
    if args.split == "parity":
        if args.task_ids is not None or args.limit is not None:
            parser.error("--split parity contains exactly 30 tasks and takes no --limit or --task-ids")
        manifest = Path(__file__).with_name("tasks.parity30.yaml").read_text()
        args.task_ids = re.findall(r"^  - (task_[0-9]{4})(?: +#.*)?$", manifest, re.MULTILINE)
        if len(args.task_ids) != 30 or len(set(args.task_ids)) != 30:
            parser.error("Invalid packaged parity manifest")
        args.output_dir = args.output_dir or DEFAULT_OUTPUT_DIR.with_name("gamedevbench-parity")
        expected = {"gamedevbench-" + task.replace("_", "-") for task in args.task_ids}
        if any(p.parent.name not in expected for p in args.output_dir.glob("*/task.toml")):
            parser.error("Output already contains tasks outside the parity split")
    else:
        args.output_dir = args.output_dir or DEFAULT_OUTPUT_DIR

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
