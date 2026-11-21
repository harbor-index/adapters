import argparse
import os

from .adapter import StableToolBenchAdapter


def main():
    parser = argparse.ArgumentParser(description="Generate StableToolBench development tasks")
    parser.add_argument("--output-dir", default="datasets/stabletoolbench")
    parser.add_argument("--upstream-dir", default=os.getenv("STB_UPSTREAM_DIR"))
    parser.add_argument("--toolenv-dir", default=os.getenv("STB_TOOLENV_DIR"))
    parser.add_argument("--baseline-archive", default=os.getenv("STB_BASELINE_ARCHIVE"),
                        help="Local copy of the pinned official data_baselines.zip")
    parser.add_argument("--task-ids", nargs="+", help="Query IDs or generated directory names")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    if args.task_ids:
        args.task_ids = [int(item.removeprefix("g1-instruction-")) for item in args.task_ids]
    StableToolBenchAdapter(**vars(args)).run()


if __name__ == "__main__":
    main()
