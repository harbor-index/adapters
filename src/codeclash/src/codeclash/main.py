"""Generate CodeClash arena tasks for Harbor from native PvP configurations."""

import argparse
from pathlib import Path
from .adapter import CodeClashAdapter
from .catalog import ARENAS


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", type=Path, default=Path.cwd() / "datasets/codeclash"
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--task-ids", nargs="+")
    parser.add_argument(
        "--profile", choices=("benchmark", "smoke"), default="benchmark"
    )
    parser.add_argument(
        "--config",
        type=Path,
        action="append",
        help="Native PvP YAML/JSON; repeat for distinct arenas",
    )
    parser.add_argument(
        "--config-root",
        type=Path,
        help="Base directory for native !include directives (normally upstream configs/)",
    )
    parser.add_argument("--list-arenas", action="store_true")
    args = vars(parser.parse_args())
    if args.pop("list_arenas"):
        for slug, value in sorted(ARENAS.items()):
            print(f"{slug}\t{value['name']}\t{value['config_origin']}")
        return
    try:
        paths = CodeClashAdapter(**args).run()
    except (ValueError, FileExistsError, OSError) as error:
        parser.exit(2, f"{error}\n")
    for path in paths:
        print(path)


if __name__ == "__main__":
    main()
