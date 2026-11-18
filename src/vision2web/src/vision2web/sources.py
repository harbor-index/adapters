"""Fetch pinned inputs and check source integrity before generating tasks."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path, PurePosixPath

from .pins import ARCHIVES, DATASET_REPO, DATASET_REVISION, LEVELS, UPSTREAM_REVISION

PACKAGE_DIR = Path(__file__).parent


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def catalog() -> list[dict]:
    return json.loads((PACKAGE_DIR / "data/catalog.json").read_text())["tasks"]


def check_upstream(source: Path) -> None:
    manifest = json.loads((PACKAGE_DIR / "data/upstream-files.json").read_text())
    for relative, expected in manifest["files"].items():
        path = source / relative
        if not path.is_file() or path.is_symlink() or sha256(path) != expected:
            raise ValueError(f"Upstream source differs from {UPSTREAM_REVISION}: {relative}")


def fetch_upstream(destination: Path) -> None:
    if not destination.exists():
        subprocess.run(
            [
                "git",
                "clone",
                "--no-checkout",
                "--filter=blob:none",
                "https://github.com/zai-org/Vision2Web.git",
                str(destination),
            ],
            check=True,
        )
        subprocess.run(["git", "-C", str(destination), "checkout", UPSTREAM_REVISION], check=True)
    check_upstream(destination)


def extract_archive(archive: Path, destination: Path, level: str) -> None:
    """Copy regular files only. Never follow archive paths or symlinks outside the cache."""
    with tarfile.open(archive, "r:gz") as source:
        for item in source:
            relative = PurePosixPath(item.name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"Unsafe archive path: {item.name}")
            if not relative.parts or relative.parts[0] != level:
                raise ValueError(f"Unexpected archive root: {item.name}")
            if relative.name == ".DS_Store" or relative.name.startswith("._"):
                continue
            if item.issym() or item.islnk() or not (item.isdir() or item.isfile()):
                raise ValueError(f"Unsupported archive entry: {item.name}")
            target = destination / str(relative)
            if target.is_symlink() or any(p.is_symlink() for p in target.parents):
                raise ValueError(f"Symlink in extraction destination: {target}")
            if item.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif item.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(item) as incoming, target.open("wb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing)


def fetch_dataset(destination: Path) -> None:
    """Download the three complete archives; this transfers about 4.11 GB."""
    archive_dir = destination / ".archives"
    archive_dir.mkdir(parents=True, exist_ok=True)
    for level in LEVELS:
        size, checksum = ARCHIVES[level]
        archive = archive_dir / f"{level}.tar.gz"
        if not archive.exists() or archive.stat().st_size != size or sha256(archive) != checksum:
            url = (
                f"https://huggingface.co/datasets/{DATASET_REPO}/resolve/"
                f"{DATASET_REVISION}/archives/{level}.tar.gz"
            )
            temporary = archive.with_suffix(".part")
            print(f"Downloading {level}: {size / 1e9:.2f} GB", flush=True)
            with urllib.request.urlopen(url, timeout=120) as response, temporary.open("wb") as out:
                shutil.copyfileobj(response, out, length=4 * 1024 * 1024)
            if temporary.stat().st_size != size or sha256(temporary) != checksum:
                raise ValueError(f"Downloaded archive failed its pinned checksum: {level}")
            temporary.replace(archive)
        marker = destination / f".{level}-{DATASET_REVISION}.json"
        if not marker.exists():
            print(f"Extracting verified {level} archive", flush=True)
            extract_archive(archive, destination, level)
            marker.write_text(json.dumps({"archive_sha256": checksum}) + "\n")
