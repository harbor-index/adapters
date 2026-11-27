"""CLI for the SWEeper-Bench Harbor adapter.

Run from src/sweeper-bench/:
    uv run sweeper-bench --output-dir ../../datasets/sweeper-bench
    uv run sweeper-bench --output-dir /tmp/sweeper --task-ids sweeper-001 sweeper-073
    uv run sweeper-bench --output-dir /tmp/sweeper --split pilot
"""

import argparse
from pathlib import Path

from .adapter import DATASET_REVISION, IMAGE_REGISTRY, IMAGE_TAG, SPLITS, SWEeperBenchAdapter

# Default output dir: <repo>/datasets/<adapter_id>
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "datasets" / "sweeper-bench"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Harbor tasks from SWEeper-Bench")
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
        help="Only generate these task IDs (upstream case ids, e.g. sweeper-001)",
    )
    parser.add_argument(
        "--split",
        choices=sorted(SPLITS),
        default=None,
        help="Generate a fixed subset of task ids",
    )
    parser.add_argument(
        "--dataset-file",
        type=Path,
        default=None,
        help="Local copy of data/test.jsonl (default: download the pinned HF revision)",
    )
    parser.add_argument(
        "--dataset-revision",
        default=DATASET_REVISION,
        help="Hugging Face dataset revision (commit SHA)",
    )
    parser.add_argument("--image-registry", default=IMAGE_REGISTRY, help="Registry/owner of the upstream images")
    parser.add_argument("--image-tag", default=IMAGE_TAG, help="Tag of the upstream images")
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache for reference patches (default ~/.cache/harbor-sweeper-bench)",
    )
    args = parser.parse_args()

    adapter = SWEeperBenchAdapter(
        args.output_dir,
        overwrite=args.overwrite,
        limit=args.limit,
        task_ids=args.task_ids,
        split=args.split,
        dataset_file=args.dataset_file,
        dataset_revision=args.dataset_revision,
        image_registry=args.image_registry,
        image_tag=args.image_tag,
        cache_dir=args.cache_dir,
    )

    adapter.run()


if __name__ == "__main__":
    main()
