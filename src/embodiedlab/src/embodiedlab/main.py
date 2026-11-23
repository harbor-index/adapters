"""Generate the EmbodiedLab Harbor tasks (T0 interface level, one dev instance per experiment)."""
import argparse
import os
from pathlib import Path

from .adapter import SOURCE_REF, EmbodiedLabAdapter


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True, help="where the task directories are written")
    parser.add_argument("--limit", type=int, help="generate at most this many tasks")
    parser.add_argument("--overwrite", action="store_true", help="replace task directories that already exist")
    parser.add_argument("--task-ids", nargs="+",
                        help="experiments to generate, as task names (hooke-k) or experiment ids (hooke_k)")
    parser.add_argument("--split", choices=["full", "parity"], default="full",
                        help="parity: only the parity subset (see README)")
    parser.add_argument("--source-dir", type=Path,
                        help=f"an EmbodiedLab checkout (default: clone the {SOURCE_REF} release from GitHub)")
    parser.add_argument("--source-ref", default=SOURCE_REF, help="the release tag to clone without --source-dir")
    parser.add_argument("--oracle-bundle",
                        help="the private oracle bundle (the file named in oracle.json): a local path or an https "
                             "link; also read from EMBODIEDLAB_ORACLE_BUNDLE. Without it (or --oracle-dir) the tasks "
                             "are generated without their oracle replay")
    parser.add_argument("--oracle-dir", type=Path,
                        help="directory with the recorded oracle runs (<experiment>.jsonl.xz), instead of the bundle")
    parser.add_argument("--image", help="the shared task image (default: embodiedlab/harbor:<benchmark version>); "
                                        "use a registry reference for cloud sandboxes")
    parser.add_argument("--image-context", type=Path,
                        help="where the image's build context is written (default: <output-dir>-image)")
    parser.add_argument("--cache-dir", type=Path,
                        help="clones, downloads and reference optima (default: ~/.cache/harbor-embodiedlab)")
    parser.add_argument("--all-experiments", action="store_true",
                        help="also generate the experiments that have no oracle replay (see README)")
    args = parser.parse_args()
    try:
        tasks = EmbodiedLabAdapter(**vars(args)).run()
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(f"Generated {len(tasks)} EmbodiedLab tasks in {args.output_dir}")
    if not (args.oracle_bundle or args.oracle_dir or os.environ.get("EMBODIEDLAB_ORACLE_BUNDLE")):
        print("No oracle bundle given: the tasks carry no oracle replay (solution/solve.sh exits with an error).")


if __name__ == "__main__":
    main()
