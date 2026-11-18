"""Generate CWE-Bench tasks from the pinned release or a local checkout."""

import argparse
from pathlib import Path

from huggingface_hub import snapshot_download

from .adapter import CWEBenchAdapter

DATASET_REPO = "collinear-ai/cwe-bench"
DATASET_REVISION = "d80ebe1014b49a147e77a09d67fd0a4101333ad6"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate CWE-Bench tasks with separate verifiers.")
    parser.add_argument("--source-dir", type=Path, help="Use a local checkout instead of downloading CWE-Bench v2.0.1")
    parser.add_argument(
        "--output-dir", type=Path, default=Path("datasets/cwe-bench"), help="Output directory (default: %(default)s)"
    )
    parser.add_argument("--limit", type=int, help="Maximum number of tasks to generate")
    parser.add_argument("--overwrite", action="store_true", help="Replace existing generated task directories")
    parser.add_argument("--task-ids", nargs="+", help="Generate only the specified upstream task directories")
    args = parser.parse_args()

    source_dir = args.source_dir
    if source_dir is None:
        source_dir = Path(snapshot_download(
            repo_id=DATASET_REPO,
            repo_type="dataset",
            revision=DATASET_REVISION,
            allow_patterns="tasks/*",
        ))

    generated = CWEBenchAdapter(
        args.output_dir,
        source_dir,
        limit=args.limit,
        overwrite=args.overwrite,
        task_ids=args.task_ids,
    ).run()
    print(f"Generated {len(generated)} CWE-Bench tasks in {args.output_dir}")


if __name__ == "__main__":
    main()
