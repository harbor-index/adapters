"""Generate DCP-Bench-Open v0.3.0 tasks with the native CPMpy evaluator."""
import argparse
from pathlib import Path
from .adapter import DCPBenchOpenAdapter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, required=True, help="Expanded pinned v0.3.0 source")
    parser.add_argument("--solver-image-archive", type=Path, help="docker save of dcp-eval/cpmpy_python:v1")
    parser.add_argument("--solver-archive-sha256", help="Expected archive checksum; enables offline generation with --verifier-image")
    parser.add_argument("--solver-image-id", required=True, help="docker image inspect --format '{{.Id}}' identity")
    parser.add_argument("--docker-binary-image", default="docker:28.2.2-dind")
    parser.add_argument("--verifier-image", help="Use an already prepared shared verifier base image")
    parser.add_argument("--split", choices=("full", "pilot"), help="Defaults to full, or pilot for control runs")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--task-ids", nargs="+")
    parser.add_argument("--control", choices=("correct", "incorrect", "unsat", "suboptimal"), default="correct")
    parser.add_argument("--controls-dir", type=Path)
    DCPBenchOpenAdapter(**vars(parser.parse_args())).run()


if __name__ == "__main__":
    main()
