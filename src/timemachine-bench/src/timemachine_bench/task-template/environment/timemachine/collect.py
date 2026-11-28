"""Collect the agent's edits to /work for the separate verifier.

Upstream runs each test in a fresh container of the task image with the agent's
copy of the repository mounted at /work, and its agent can only edit files that
already exist. Harbor grades in a fresh container of the same image, so only the
edited file contents need to cross over.

  --write-baseline  (image build) record the sha256 of every regular file under
                    /work in /opt/timemachine/baseline.json.
  default           ([[verifier.collect]] hook, agent container) pack every
                    baseline file whose content changed into
                    /logs/artifacts/timemachine/changes.tar and list changed,
                    deleted and new files in summary.json.

The verifier decides which of the packed files it accepts; this script only
reports what changed.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tarfile
from pathlib import Path

WORK = Path("/work")
BASELINE = Path("/opt/timemachine/baseline.json")
OUT = Path("/logs/artifacts/timemachine")
# Directories whose contents are never part of a submission; they are skipped
# only when listing new files for the summary.
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".git", ".tox", ".venv"}
MAX_LISTED = 2000


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def regular_files(root: Path, skip_dirs: set[str] | None = None):
    for dirpath, dirnames, filenames in os.walk(root):
        if skip_dirs:
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                continue
            yield path


def write_baseline() -> int:
    manifest = {path.relative_to(WORK).as_posix(): sha256(path) for path in regular_files(WORK)}
    BASELINE.write_text(json.dumps(manifest, sort_keys=True))
    print(f"baseline: {len(manifest)} files under {WORK}")
    return 0


def collect() -> int:
    baseline: dict[str, str] = json.loads(BASELINE.read_text())
    changed: list[str] = []
    deleted: list[str] = []
    for rel, digest in sorted(baseline.items()):
        path = WORK / rel
        if path.is_symlink() or not path.is_file():
            deleted.append(rel)
        elif sha256(path) != digest:
            changed.append(rel)

    new: list[str] = []
    for path in regular_files(WORK, SKIP_DIRS):
        rel = path.relative_to(WORK).as_posix()
        if rel not in baseline:
            new.append(rel)
            if len(new) >= MAX_LISTED:
                break

    OUT.mkdir(parents=True, exist_ok=True)
    with tarfile.open(OUT / "changes.tar", "w", format=tarfile.PAX_FORMAT) as tar:
        for rel in changed:
            tar.add(WORK / rel, arcname=rel, recursive=False)
    (OUT / "summary.json").write_text(
        json.dumps(
            {"changed": changed, "deleted": deleted, "new": sorted(new)},
            indent=2,
        )
    )
    print(
        f"collected {len(changed)} changed file(s); "
        f"{len(deleted)} deleted and {len(new)} new file(s) are not submitted"
    )
    return 0


if __name__ == "__main__":
    sys.exit(write_baseline() if "--write-baseline" in sys.argv[1:] else collect())
