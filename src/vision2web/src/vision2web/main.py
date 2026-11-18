"""Generate all 193 tasks, or a reproducible selection, without making model calls."""

import argparse
import json
from pathlib import Path

from .adapter import Vision2WebAdapter
from .pins import LEVELS, RUNTIME_IMAGE
from .sources import fetch_dataset, fetch_upstream


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    cache = Path.home() / ".cache/harbor-vision2web"
    parser.add_argument("--output-dir", type=Path, default=Path("datasets/vision2web"))
    parser.add_argument("--limit", type=int)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--task-ids", nargs="+")
    parser.add_argument("--split", choices=["full", "pilot", *LEVELS], default="full")
    parser.add_argument("--dataset-dir", type=Path, default=cache / "datasets")
    parser.add_argument("--upstream-dir", type=Path, default=cache / "upstream")
    parser.add_argument(
        "--reference-dir",
        type=Path,
        help="Optional candidate sites under <level>/<task>/; no oracle shortcut",
    )
    parser.add_argument("--runtime-image", default=RUNTIME_IMAGE)
    parser.add_argument(
        "--memory-mb",
        type=int,
        default=8192,
        help="Provisional per-container cap; record identically on both sides",
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="Fetch complete pinned sources (~4.11 GB compressed task data)",
    )
    parser.add_argument("--list", action="store_true", help="List IDs without downloading")
    args = parser.parse_args()
    if args.memory_mb < 1:
        parser.error("--memory-mb must be positive")
    adapter = Vision2WebAdapter(
        output_dir=args.output_dir,
        dataset_dir=args.dataset_dir,
        upstream_dir=args.upstream_dir,
        limit=args.limit,
        overwrite=args.overwrite,
        task_ids=args.task_ids,
        split=args.split,
        reference_dir=args.reference_dir,
        runtime_image=args.runtime_image,
        memory_mb=args.memory_mb,
    )
    try:
        if args.list:
            print(json.dumps([r["id"] for r in adapter.selected()], indent=2))
            return
        if args.download:
            fetch_upstream(args.upstream_dir)
            fetch_dataset(args.dataset_dir)
        tasks = adapter.run()
    except (ValueError, FileNotFoundError, FileExistsError) as error:
        parser.exit(1, str(error) + "\n")
    print(json.dumps({"generated": len(tasks), "output_dir": str(adapter.output_dir)}))


if __name__ == "__main__":
    main()
