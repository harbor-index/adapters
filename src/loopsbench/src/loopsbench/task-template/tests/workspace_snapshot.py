"""Restore an untrusted LoopsBench workspace archive inside the verifier."""

import os
import posixpath
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath


def archive_path(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Archive path escapes the workspace: {!r}".format(name))
    return path


def validated_members(archive, workspace):
    entries = {}
    for member in archive:
        path = archive_path(member.name)
        if not path.parts and member.isdir():
            continue
        if not path.parts or path in entries:
            raise ValueError("Invalid or duplicate archive path: {!r}".format(member.name))
        if member.issym():
            link = PurePosixPath(member.linkname)
            target = link if link.is_absolute() else workspace / path.parent / link
            normalized = PurePosixPath(posixpath.normpath(str(target)))
            if normalized != workspace and workspace not in normalized.parents:
                # Some task images expose a test file at a fixed workspace path.
                # Preserve a link outside the workspace only if the fresh image
                # already has that exact link. The ancestor check below still
                # forbids extracting any archive entries through it.
                baked_link = workspace / path
                if not baked_link.is_symlink() or os.readlink(str(baked_link)) != member.linkname:
                    raise ValueError("Archive symlink leaves the workspace: {!r}".format(member.name))
        elif member.islnk():
            target = archive_path(member.linkname)
            if target not in entries or not entries[target].isfile():
                raise ValueError("Archive hard link must target an earlier file: {!r}".format(member.name))
            member.linkname = target.as_posix()
        elif not (member.isdir() or member.isfile()):
            raise ValueError("Unsupported workspace archive entry: {!r}".format(member.name))
        member.name = path.as_posix()
        member.mode &= 0o777
        member.uid = os.geteuid()
        member.gid = os.getegid()
        member.uname = member.gname = ""
        entries[path] = member
    for path in entries:
        if any(parent in entries and not entries[parent].isdir() for parent in path.parents):
            raise ValueError("Archive entry is nested below a non-directory: {!r}".format(str(path)))
    return entries.values()


def restore(archive_pathname, workspace):
    workspace = Path(workspace)
    if not workspace.is_absolute() or workspace.resolve() != workspace or not workspace.is_dir():
        raise ValueError("Workspace must be a real directory at a canonical absolute path")
    with tempfile.TemporaryDirectory(prefix=".loopsbench-snapshot-", dir=str(workspace.parent)) as directory:
        staging = Path(directory)
        with tarfile.open(archive_pathname, "r:gz") as archive:
            members = validated_members(archive, workspace)
            options = {"filter": "fully_trusted"} if hasattr(tarfile, "data_filter") else {}
            archive.extractall(str(staging), members=members, **options)
        for child in workspace.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(str(child))
            else:
                child.unlink()
        for child in staging.iterdir():
            shutil.move(str(child), str(workspace / child.name))


if __name__ == "__main__":
    restore(sys.argv[1], sys.argv[2])
