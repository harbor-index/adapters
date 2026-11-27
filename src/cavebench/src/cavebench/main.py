"""
Main entry point for the CaveBench adapter. Do not modify any of the existing flags.
You can add any additional flags you need.

Constructs the Adapter class defined in adapter.py and calls run() to generate tasks
in the Harbor format at the configured output directory.
"""

import argparse
from pathlib import Path

from .adapter import CaveBenchAdapter

# Default output dir: <repo>/datasets/<adapter_id>
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[4] / "datasets" / "cavebench"


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
        help="Only generate these task IDs (s01-sample-0001-k2 or scene_01/sample_0001/k2)",
    )
    parser.add_argument(
        "--split",
        choices=["full", "parity"],
        default="full",
        help="full: all 1,180 test questions; parity: the 118-question parity subset",
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=None,
        help="Local copy of the pinned HF files (questions.jsonl, labels.jsonl, "
        "test_frames_manifest.json); default: download into ~/.cache/harbor-cavebench",
    )
    args = parser.parse_args()

    adapter = CaveBenchAdapter(
        args.output_dir,
        overwrite=args.overwrite,
        limit=args.limit,
        task_ids=args.task_ids,
        split=args.split,
        source_dir=args.source_dir,
    )

    adapter.run()


if __name__ == "__main__":
    main()
