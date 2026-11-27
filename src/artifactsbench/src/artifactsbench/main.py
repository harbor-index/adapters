import argparse
from pathlib import Path

from .adapter import ArtifactsBenchAdapter


def main():
    parser = argparse.ArgumentParser(description="Generate tasks from the pinned ArtifactsBench dataset.")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--dataset", type=Path, help="Local pinned artifacts_bench.json (JSON Lines)")
    parser.add_argument("--task-ids", type=int, nargs="+", help="One or more original numeric indices")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--split", choices=("full", "parity"), default="full",
                        help="The parity split is a proposed, reproducible 60-task sample")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--reference-dir", type=Path, help="Optional reference answers named NNNN.md")
    args = parser.parse_args()
    try:
        count = ArtifactsBenchAdapter(**vars(args)).run()
    except (ValueError, OSError) as error:
        parser.exit(1, f"artifactsbench: {error}\n")
    print(f"Generated {count} tasks in {args.output_dir}")


if __name__ == "__main__":
    main()
