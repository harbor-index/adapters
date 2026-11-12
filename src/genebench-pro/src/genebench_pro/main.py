"""Generate Harbor tasks from the pinned GeneBench-Pro public package."""

import argparse
from pathlib import Path

from .adapter import DATASET_REVISION, GeneBenchProAdapter

# Default output dir: <repo>/datasets/<adapter_id>
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "datasets" / "genebench-pro"


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
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR.parents[1] / "tmp" / "genebench-pro" / "huggingface",
        help="Hugging Face download cache",
    )
    parser.add_argument(
        "--revision",
        default=DATASET_REVISION,
        help="Full commit SHA of the OpenAI public package (default: verified release)",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Generate from the selected snapshot already in the cache",
    )
    args = parser.parse_args()

    adapter = GeneBenchProAdapter(
        args.output_dir,
        overwrite=args.overwrite,
        limit=args.limit,
        task_ids=args.task_ids,
        cache_dir=args.cache_dir,
        offline=args.offline,
        revision=args.revision,
    )

    adapter.run()


if __name__ == "__main__":
    main()
