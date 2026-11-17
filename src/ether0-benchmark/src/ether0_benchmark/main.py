"""
CLI entry point for the ether0-benchmark adapter.

Generates Harbor tasks from the pinned ether0-benchmark test split:

    uv run ether0-benchmark --output-dir ../../datasets/ether0-benchmark
"""

import argparse
from pathlib import Path

from .adapter import Ether0BenchmarkAdapter

# Default output dir: <repo>/datasets/<adapter_id>
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "datasets" / "ether0-benchmark"


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
        help="Generate only the first N tasks (in sorted task-id order)",
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
        help="Only generate these task IDs (upstream problem UUIDs)",
    )
    parser.add_argument(
        "--parquet-path",
        type=Path,
        default=None,
        help=(
            "Optional local copy of the test-split parquet; by default the pinned "
            "revision is downloaded from Hugging Face"
        ),
    )
    parser.add_argument(
        "--docker-image",
        default=None,
        help=(
            "Optional prebuilt image (built from task-template/environment/Dockerfile) "
            "to set as [environment].docker_image; by default each task builds its Dockerfile"
        ),
    )
    args = parser.parse_args()

    adapter = Ether0BenchmarkAdapter(
        args.output_dir,
        overwrite=args.overwrite,
        limit=args.limit,
        task_ids=args.task_ids,
        parquet_path=args.parquet_path,
        docker_image=args.docker_image,
    )

    adapter.run()


if __name__ == "__main__":
    main()
