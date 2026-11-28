"""Generate the released Blocksworld Hard tasks."""

import argparse
from pathlib import Path

from .adapter import BlocksworldHardAdapter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--source-dir", type=Path, help="Local checkout of the pinned upstream revision"
    )
    parser.add_argument("--limit", type=int)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--task-ids", nargs="+")
    parser.add_argument("--split", choices=["full", "parity"], default="full")
    BlocksworldHardAdapter(**vars(parser.parse_args())).run()


if __name__ == "__main__":
    main()
