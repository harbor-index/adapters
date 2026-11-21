import argparse
import os
from pathlib import Path
from .adapter import StressWebAdapter

def main():
    parser = argparse.ArgumentParser(description="Generate pinned StressWeb Note tasks.")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--task-ids", nargs="+")
    parser.add_argument("--split", choices=["note", "parity"], default="note")
    parser.add_argument("--cache-dir", type=Path, default=Path(os.environ.get("XDG_CACHE_HOME",
                        Path.home() / ".cache")) / "harbor-stressweb")
    args = parser.parse_args()
    StressWebAdapter(args.output_dir, args.cache_dir, args.task_ids, args.limit, args.overwrite).run()

if __name__ == "__main__":
    main()
