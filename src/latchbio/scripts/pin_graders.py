#!/usr/bin/env python3
"""Maintain ``graders.lock.json``, the hashes of the latch-eval-tools files the tasks rely on.

The adapter does not copy LatchBio's graders. It installs the PyPI release
pinned in ``latchbio.sources`` and records the SHA-256 of the grader modules and
of the prompt suffix; the task verifier refuses to grade with anything else.

    uv run python scripts/pin_graders.py --check
    uv run python scripts/pin_graders.py --check --source /path/to/latch-eval-tools
    uv run python scripts/pin_graders.py --write        # after changing the pinned release

``--check`` compares the installed release with the lock file. With ``--source``
(a git checkout of latch-eval-tools at the pinned commit) it also compares the
checkout, which ties the PyPI release to the commit recorded in the lock file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from importlib import metadata
from pathlib import Path

ADAPTER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADAPTER_ROOT / "src"))

from latchbio.graders_release import DATA_INSTRUCTIONS, LOCK_PATH, load_lock, mismatches, package_dir  # noqa: E402
from latchbio.sources import (  # noqa: E402
    LATCH_EVAL_TOOLS_COMMIT,
    LATCH_EVAL_TOOLS_REPO,
    LATCH_EVAL_TOOLS_VERSION,
)

NOTE = (
    "SHA-256 of the latch-eval-tools files the Harbor tasks rely on. `files` are the "
    "modules the verifier imports (checked by tests/grade.py before grading); "
    "`prompt_files` is the prompt suffix the adapter appends to every instruction. "
    "Regenerate with scripts/pin_graders.py."
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def installed_version() -> str:
    return metadata.version("latch-eval-tools")


def write() -> None:
    root = package_dir()
    modules = sorted(path for path in (root / "graders").glob("*.py")) + [root / "types.py"]
    lock = {
        "package": "latch-eval-tools",
        "version": installed_version(),
        "upstream_repo": LATCH_EVAL_TOOLS_REPO,
        "upstream_commit": LATCH_EVAL_TOOLS_COMMIT,
        "note": NOTE,
        "files": {path.relative_to(root).as_posix(): sha256(path) for path in modules},
        "prompt_files": {DATA_INSTRUCTIONS: sha256(root / DATA_INSTRUCTIONS)},
    }
    LOCK_PATH.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {LOCK_PATH} for latch-eval-tools {lock['version']} ({len(lock['files'])} modules)")


def check(source: Path | None) -> int:
    lock = load_lock()
    files = {**lock["files"], **lock["prompt_files"]}
    problems = []
    if lock["version"] != LATCH_EVAL_TOOLS_VERSION or lock["upstream_commit"] != LATCH_EVAL_TOOLS_COMMIT:
        problems.append("lock file and latchbio.sources name different releases")
    if installed_version() != lock["version"]:
        problems.append(f"latch-eval-tools {installed_version()} is installed, the lock pins {lock['version']}")
    problems += [f"installed file differs from the lock: {rel}" for rel in mismatches(files)]
    on_disk = {path.relative_to(package_dir()).as_posix() for path in (package_dir() / "graders").glob("*.py")}
    problems += [f"grader module not in the lock: {rel}" for rel in sorted(on_disk - set(lock["files"]))]
    if source is not None:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=source, check=True, capture_output=True, text=True
        ).stdout.strip()
        if head != lock["upstream_commit"]:
            problems.append(f"{source} is at {head}, the lock pins {lock['upstream_commit']}")
        checkout = source / "src" / "latch_eval_tools"
        problems += [f"checkout file differs from the lock: {rel}" for rel in mismatches(files, checkout)]
    for problem in problems:
        print(f"MISMATCH: {problem}", file=sys.stderr)
    if not problems:
        scope = "installed release and the upstream checkout" if source else "installed release"
        print(f"OK: {len(files)} files of latch-eval-tools {lock['version']} match the lock ({scope})")
    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Verify the installed release against the lock")
    mode.add_argument("--write", action="store_true", help="Rewrite the lock from the installed release")
    parser.add_argument("--source", type=Path, default=None, help="Also verify a checkout of latch-eval-tools")
    args = parser.parse_args()
    if args.write:
        write()
        return 0
    return check(args.source)


if __name__ == "__main__":
    sys.exit(main())
