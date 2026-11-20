"""Safe workspace snapshotting and upstream-faithful copy filters.

Preserves exact upstream exclusion sets while rejecting symlink escapes and
special files (FIFOs/sockets/devices) outside the allow-listed workspace root.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from pathlib import Path

# Exact upstream Backend/scenario1 exclusions (eval_task.py L81-83).
BACKEND_EXCLUDED_COPY_DIRS = frozenset(
    {".git", ".hg", ".svn", "__pycache__", ".sii", "logs", "bin"}
)
BACKEND_EXCLUDED_COPY_FILES = frozenset({"meta_eval.json"})
BACKEND_EXCLUDED_COPY_SUFFIXES = frozenset({".db", ".db3", ".sqlite", ".sqlite3", ".txt"})


class WorkspaceSecurityError(RuntimeError):
    """Raised when a workspace entry attempts to escape the allowed root."""


def clear_directory(path: Path) -> None:
    path = path.resolve()
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        return
    for entry in path.iterdir():
        if entry.is_symlink() or entry.is_file():
            entry.unlink()
        elif entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()


def _validate_entry_within_root(entry: Path, src_root: Path) -> None:
    """Reject symlinks escaping src_root and non-regular files."""
    st = os.lstat(entry)
    if stat.S_ISLNK(st.st_mode):
        resolved = entry.resolve()
        try:
            resolved.relative_to(src_root)
        except ValueError as exc:
            raise WorkspaceSecurityError(
                f"Refusing symlink escaping workspace root: {entry} -> {resolved}"
            ) from exc
    elif not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
        raise WorkspaceSecurityError(f"Refusing non-regular workspace entry: {entry}")


def safe_copy_workspace(
    src: Path,
    dst: Path,
    *,
    skip_hidden: bool = True,
    excluded_dirs: frozenset[str] = frozenset(),
    excluded_files: frozenset[str] = frozenset(),
    excluded_suffixes: frozenset[str] = frozenset(),
    preserve_empty_dirs: bool = True,
) -> list[str]:
    """Copy workspace tree safely, rejecting symlink escapes and special files.

    Returns a sorted list of copied relative POSIX file paths.
    """
    src_root = src.resolve()
    dst_root = dst.resolve()
    if not src_root.is_dir():
        raise FileNotFoundError(f"Source workspace missing: {src_root}")

    dst_root.mkdir(parents=True, exist_ok=True)
    clear_directory(dst_root)

    copied: list[str] = []
    for root, dirs, files in os.walk(src_root, followlinks=False):
        root_path = Path(root)
        rel_root = root_path.relative_to(src_root)

        if any(
            (skip_hidden and part.startswith(".")) or part in excluded_dirs
            for part in rel_root.parts
        ):
            dirs[:] = []
            continue

        kept_dirs: list[str] = []
        for dname in sorted(dirs):
            if (skip_hidden and dname.startswith(".")) or dname in excluded_dirs:
                continue
            dpath = root_path / dname
            _validate_entry_within_root(dpath, src_root)
            if dpath.is_symlink():
                # Never traverse directory symlinks to prevent loops or leaks
                raise WorkspaceSecurityError(f"Refusing directory symlink in workspace: {dpath}")
            kept_dirs.append(dname)
        dirs[:] = kept_dirs

        target_root = dst_root / rel_root
        if preserve_empty_dirs:
            target_root.mkdir(parents=True, exist_ok=True)

        for fname in sorted(files):
            if skip_hidden and fname.startswith("."):
                continue
            if fname in excluded_files:
                continue
            if Path(fname).suffix.lower() in excluded_suffixes:
                continue
            src_file = root_path / fname
            _validate_entry_within_root(src_file, src_root)
            target_root.mkdir(parents=True, exist_ok=True)
            dst_file = target_root / fname
            shutil.copy2(src_file, dst_file)
            copied.append((rel_root / fname).as_posix() if str(rel_root) != "." else fname)

    return sorted(copied)


def safe_copy_backend_filtered(src: Path, dst: Path) -> list[str]:
    """Exact Backend/scenario1 `copy_workspace_filtered` semantics + symlink protection."""
    return safe_copy_workspace(
        src,
        dst,
        skip_hidden=True,
        excluded_dirs=BACKEND_EXCLUDED_COPY_DIRS,
        excluded_files=BACKEND_EXCLUDED_COPY_FILES,
        excluded_suffixes=BACKEND_EXCLUDED_COPY_SUFFIXES,
    )


def safe_copy_full(src: Path, dst: Path) -> list[str]:
    """Exact Code/scenario1 `copy_workspace` semantics (skip dotfiles & __pycache__) + symlink protection."""
    return safe_copy_workspace(src, dst, skip_hidden=True, excluded_dirs=frozenset({"__pycache__"}))


def hash_directory(root: Path) -> str:
    """Deterministic SHA-256 digest of all regular files under root."""
    root = root.resolve()
    h = hashlib.sha256()
    if not root.exists():
        return h.hexdigest()
    for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()):
        rel = path.relative_to(root).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()
