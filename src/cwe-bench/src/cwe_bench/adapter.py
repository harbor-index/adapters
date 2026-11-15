"""CWE-bench adapter for Harbor.

CWE-bench (Collinear AI, arXiv:2610.17020) is distributed as native Harbor tasks in
the gated Hugging Face dataset ``collinear-ai/cwe-bench``. Because the upstream
packages are already Harbor tasks, this adapter does not translate anything: it pins
a released upstream revision, fetches the task packages, and re-emits them with the
registry identity used by this repository. Instructions, environments, verifiers and
reference solutions are copied byte-for-byte.

The only file the adapter edits is ``task.toml``, and only its ``[task]`` table:
``name`` becomes ``collinear-ai/cwe-bench__<id>`` (upstream ships
``collinear-ai/<id>``), the paper's authors are appended to the upstream ``authors``
list, and ``keywords`` is filled in only if upstream sets none. Every other key and
table is left exactly as upstream wrote it, and the adapter verifies that after each
rewrite. Next to the task directories it writes the upstream ``LICENSE`` and
``NOTICE`` and a record of what was generated (``ADAPTER_NOTICE.txt``).
"""

from __future__ import annotations

import copy
import errno
import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import stat
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import tomlkit
from huggingface_hub import HfApi, snapshot_download
from huggingface_hub.errors import (
    GatedRepoError,
    HfHubHTTPError,
    RepositoryNotFoundError,
    RevisionNotFoundError,
)
from huggingface_hub.hf_api import RepoFile
from tomlkit.items import AoT

logger = logging.getLogger(__name__)

HF_REPO_ID = "collinear-ai/cwe-bench"
HF_REPO_URL = f"https://huggingface.co/datasets/{HF_REPO_ID}"

# Upstream releases and the commit that upstream's registry.json pins for every task
# of that release. Per the upstream changelog, 2.0.1 differs from 2.0 (the leaderboard
# release) only by verifier fixes for two tasks so that their reference solutions
# score 1 in a plain Docker setup.
UPSTREAM_RELEASES = {
    "2.0.1": "d80ebe1014b49a147e77a09d67fd0a4101333ad6",
    "2.0": "e0224c430f47575506297a7d55d3635f46d82b2d",
}
DEFAULT_RELEASE = "2.0.1"
DEFAULT_REVISION = UPSTREAM_RELEASES[DEFAULT_RELEASE]

REGISTRY_ORG = "collinear-ai"
REGISTRY_BENCHMARK = "cwe-bench"

TASK_TEMPLATE_DIR = Path(__file__).resolve().parent / "task-template"

# Minimum files a task package needs (docs/adapters.mdx, "Required Directory
# Structures").
REQUIRED_TASK_FILES = (
    "task.toml",
    "instruction.md",
    "environment/Dockerfile",
    "solution/solve.sh",
    "tests/test.sh",
)

# Attribution files the upstream license asks redistributors to keep.
ATTRIBUTION_FILES = ("LICENSE", "NOTICE")

# Written inside a downloaded snapshot's directory once every file the adapter needs
# from the pinned commit (tasks/**, LICENSE, NOTICE) has been checked against the
# Hub's listing; lets later runs verify the copy without network.
MANIFEST_NAME = ".cwe-bench-manifest.json"

# Written at the root of the generated dataset: where it came from and what the
# adapter changed (the upstream Apache-2.0 license asks for a notice of modification).
# The two fields below describe every task directory next to the file; a run that
# would make them wrong for a task directory it leaves in place is refused.
PROVENANCE_NAME = "ADAPTER_NOTICE.txt"
_REVISION_FIELD = "Upstream revision"
_SOLUTIONS_FIELD = "Reference solutions"
_NOTICE_FIELD_RE = re.compile(
    rf"^({_REVISION_FIELD}|{_SOLUTIONS_FIELD}): (.+)$", re.MULTILINE
)

# Tasks are built below a hidden directory inside the output directory, named
# ".staging-" plus eight hex digits, and moved into place with a rename.
_STAGING_PREFIX = ".staging-"
_STAGING_NAME_RE = re.compile(re.escape(_STAGING_PREFIX) + r"[0-9a-f]{8}")

_LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/"
_IDENTITY_KEYS = ("name", "authors", "keywords")
_MAX_TREE_DEPTH = 100  # release 2.0.1 nests 11 levels at most

# Upstream ids look like "<language>-cwe<N>-<id>" with an optional "-hardened-vK".
_TASK_ID_RE = re.compile(r"^(?P<language>[a-z]+)-cwe(?P<cwe>\d+)-")
_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")
_UNSAFE_ID_CHARS_RE = re.compile(r"[^a-z0-9]+")


class CWEBenchAccessError(RuntimeError):
    """Raised when the gated upstream repository cannot be read."""


class CWEBenchSourceError(RuntimeError):
    """Raised when the upstream copy cannot be fetched or is not usable."""


@dataclass(frozen=True)
class UpstreamTask:
    """One upstream task package and the identity it gets in Harbor."""

    upstream_id: str
    local_id: str
    source_dir: Path

    @property
    def registry_name(self) -> str:
        return f"{REGISTRY_ORG}/{REGISTRY_BENCHMARK}__{self.local_id}"


def sanitize_task_id(upstream_id: str) -> str:
    """Return a registry-safe id: lowercase, runs of other characters become hyphens."""
    return _UNSAFE_ID_CHARS_RE.sub("-", upstream_id.lower()).strip("-")


def _real(path: Path) -> Path:
    """Resolve links in ``path``; unlike Path.resolve() this never fails on a loop."""
    return Path(os.path.realpath(path))


def _open_nofollow(path: str, flags: int) -> int:
    return os.open(path, flags | os.O_NOFOLLOW)


class CWEBenchAdapter:
    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        *,
        revision: str = DEFAULT_REVISION,
        source_dir: Path | None = None,
        cache_dir: Path | None = None,
        omit_solutions: bool = False,
    ):
        """
        Args:
            output_dir: Directory the generated task directories are written to.
            limit: Generate at most this many tasks (after ``task_ids`` filtering).
            overwrite: Replace task directories that already exist.
            task_ids: Only generate these tasks. Accepts upstream ids, upstream names
                (``collinear-ai/<id>``) or registry names
                (``collinear-ai/cwe-bench__<id>``).
            revision: Upstream release name (``2.0.1``, ``2.0``) or Hugging Face
                revision (commit hash, tag or branch). Not used with ``source_dir``.
            source_dir: Use an existing local copy of the upstream repository (the
                directory that contains ``tasks/``) as it is instead of downloading.
            cache_dir: Where the upstream snapshot is downloaded. Defaults to
                ``$CWE_BENCH_CACHE_DIR`` or ``~/.cache/harbor-adapters/cwe-bench``.
            omit_solutions: Write a stub instead of each upstream ``solution/`` so the
                output carries no reference fix (see README, Notes & Caveats).
        """
        self.output_dir = Path(output_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.revision = revision
        self.source_dir = Path(source_dir) if source_dir is not None else None
        self.cache_dir = (
            Path(cache_dir)
            if cache_dir is not None
            else Path(
                os.environ.get(
                    "CWE_BENCH_CACHE_DIR",
                    Path.home() / ".cache" / "harbor-adapters" / "cwe-bench",
                )
            )
        )
        self.omit_solutions = omit_solutions
        # What the tasks are generated from, as recorded in ADAPTER_NOTICE.txt: the
        # commit hash of a verified snapshot, otherwise a fingerprint of the local
        # copy. Set once the source has been checked.
        self._source_label: str | None = None

    # ------------------------------------------------------------------ source

    def _resolve_revision(self, api: HfApi) -> str:
        """Turn the configured release name or revision into a full commit hash."""
        revision = UPSTREAM_RELEASES.get(self.revision, self.revision)
        if _FULL_SHA_RE.fullmatch(revision):
            return revision
        # The Hub resolves tags, branches and abbreviated commit hashes itself.
        commit = api.dataset_info(HF_REPO_ID, revision=revision).sha
        if not commit:
            raise CWEBenchSourceError(
                f"The Hub reported no commit for revision {self.revision!r} of "
                f"{HF_REPO_ID}."
            )
        return commit

    @staticmethod
    def _list_snapshot_files(api: HfApi, commit: str) -> dict[str, int]:
        """Return ``{path: size}`` for every file the adapter needs at ``commit``."""
        files: dict[str, int] = {}
        for entry in api.list_repo_tree(
            HF_REPO_ID, repo_type="dataset", revision=commit, recursive=True
        ):
            if isinstance(entry, RepoFile) and (
                entry.path.startswith("tasks/") or entry.path in ATTRIBUTION_FILES
            ):
                files[entry.path] = entry.size
        if not any(path.startswith("tasks/") for path in files):
            raise CWEBenchSourceError(
                f"{HF_REPO_ID} at {commit} has no tasks/ directory; "
                "is this a CWE-bench release?"
            )
        return files

    @staticmethod
    def _load_manifest(local_dir: Path) -> tuple[str, dict[str, int]] | None:
        """Return ``(commit, {path: size})`` from a snapshot's manifest, if usable."""
        try:
            manifest = json.loads(
                (local_dir / MANIFEST_NAME).read_text(encoding="utf-8")
            )
        except (OSError, ValueError, RecursionError):
            return None
        if not isinstance(manifest, dict):
            return None
        commit, files = manifest.get("commit"), manifest.get("files")
        if (
            not isinstance(commit, str)
            or not _FULL_SHA_RE.fullmatch(commit)
            or not isinstance(files, dict)
            or not files
        ):
            return None
        return commit, files

    @staticmethod
    def _verify_snapshot(local_dir: Path, files: dict[str, int], advice: str) -> None:
        """Fail unless ``local_dir`` holds exactly the task files of the pinned commit."""
        problems = []
        for relative, size in sorted(files.items()):
            path = local_dir / relative
            if not path.is_file():
                problems.append(f"missing {relative}")
            elif path.stat().st_size != size:
                problems.append(f"wrong size {relative}")
        expected = set(files)
        for relative in files:
            expected.update(p.as_posix() for p in PurePosixPath(relative).parents)
        for current, subdirs, names in os.walk(local_dir / "tasks"):
            here = Path(current).relative_to(local_dir)
            extra = sorted(
                name
                for name in subdirs + names
                if (here / name).as_posix() not in expected
            )
            problems.extend(f"unexpected {(here / name).as_posix()}" for name in extra)
            subdirs[:] = [name for name in subdirs if name not in extra]
        if problems:
            shown = "; ".join(problems[:3])
            raise CWEBenchSourceError(
                f"The upstream snapshot in {local_dir} is incomplete or holds entries "
                f"that are not part of the release ({len(problems)} problem(s), e.g. "
                f"{shown}). {advice}"
            )

    def _download_snapshot(self) -> Path:
        """Download the pinned upstream revision and return its root directory."""
        api = HfApi()
        try:
            commit = self._resolve_revision(api)
            local_dir = self.cache_dir / commit
            # Before anything is fetched: the snapshot must not land in --output-dir.
            self._check_overlap(local_dir)
            manifest = self._load_manifest(local_dir)
            listed = manifest is not None and manifest[0] == commit
            if listed:
                files = manifest[1]
            else:
                files = self._list_snapshot_files(api, commit)
                logger.info("Fetching %s at %s into %s", HF_REPO_ID, commit, local_dir)
                snapshot_download(
                    repo_id=HF_REPO_ID,
                    repo_type="dataset",
                    revision=commit,
                    allow_patterns=["tasks/**", *ATTRIBUTION_FILES],
                    local_dir=local_dir,
                )
        except RevisionNotFoundError as exc:
            releases = ", ".join(UPSTREAM_RELEASES)
            raise ValueError(
                f"Revision {self.revision!r} does not exist in {HF_REPO_ID}. Use a "
                f"release name ({releases}), a commit hash, a tag or a branch."
            ) from exc
        except (GatedRepoError, RepositoryNotFoundError) as exc:
            raise CWEBenchAccessError(_ACCESS_HELP) from exc
        except HfHubHTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (401, 403):
                raise CWEBenchAccessError(_ACCESS_HELP) from exc
            raise CWEBenchSourceError(
                f"The Hugging Face Hub request failed: {exc}"
            ) from exc
        except (CWEBenchSourceError, ValueError):
            raise
        except Exception as exc:  # network and cache failures of any client library
            raise CWEBenchSourceError(
                f"Could not download {HF_REPO_ID}: {type(exc).__name__}: {exc}"
            ) from exc
        self._verify_snapshot(
            local_dir,
            files,
            "Delete that directory to download it again (removing the entries reported "
            "as unexpected is enough if nothing is missing or has the wrong size), then "
            "run the adapter again.",
        )
        if not listed:
            (local_dir / MANIFEST_NAME).write_text(
                json.dumps({"commit": commit, "files": files}, sort_keys=True),
                encoding="utf-8",
            )
        logger.info("Using the verified snapshot in %s", local_dir)
        self._source_label = commit
        return local_dir

    def _local_copy(self) -> Path:
        """Check the ``--source-dir`` copy and return it."""
        root = self.source_dir
        tasks_root = root / "tasks"
        if tasks_root.is_symlink() or not tasks_root.is_dir():
            raise FileNotFoundError(
                f"--source-dir must contain a real tasks/ directory: {root}"
            )
        self._check_overlap(root)
        manifest = self._load_manifest(root)
        if manifest is not None:
            # A snapshot this adapter downloaded and verified earlier: hold it to the
            # same standard as a fresh download.
            self._verify_snapshot(
                root,
                manifest[1],
                "Restore the missing files and remove the unexpected entries, or "
                f"remove {MANIFEST_NAME} to use the copy as it is (it is then recorded "
                "as an unverified local copy).",
            )
            self._source_label = manifest[0]
        return root

    def _check_overlap(self, source_root: Path) -> None:
        """Refuse an output directory that contains, or lies inside, the upstream copy."""
        output = _real(self.output_dir)
        source = _real(source_root)
        if output == source or output.is_relative_to(source):
            raise ValueError(
                f"--output-dir ({self.output_dir}) is inside the upstream copy "
                f"({source_root}); choose a separate directory."
            )
        if source.is_relative_to(output):
            raise ValueError(
                f"The upstream copy ({source_root}) is inside --output-dir "
                f"({self.output_dir}); choose a separate directory."
            )

    # --------------------------------------------------------------- discovery

    @staticmethod
    def discover_tasks(source_root: Path) -> list[UpstreamTask]:
        """List upstream task packages in a reproducible order."""
        tasks_root = source_root / "tasks"
        tasks: list[UpstreamTask] = []
        seen: dict[str, str] = {}
        incomplete: dict[str, list[str]] = {}
        for task_dir in sorted(tasks_root.iterdir()):
            if task_dir.name.startswith("."):
                continue
            if task_dir.is_symlink():
                raise ValueError(f"Task directory must not be a link: {task_dir}")
            if not task_dir.is_dir():
                continue
            missing = [f for f in REQUIRED_TASK_FILES if not (task_dir / f).exists()]
            if missing:
                incomplete[task_dir.name] = missing
                continue
            local_id = sanitize_task_id(task_dir.name)
            if not local_id:
                raise ValueError(f"Upstream task id {task_dir.name!r} is empty.")
            if local_id in seen:
                raise ValueError(
                    f"Upstream tasks {seen[local_id]!r} and {task_dir.name!r} both "
                    f"sanitize to {local_id!r}; task names must be unique."
                )
            seen[local_id] = task_dir.name
            tasks.append(UpstreamTask(task_dir.name, local_id, task_dir))
        if incomplete:
            details = "; ".join(f"{k}: {', '.join(v)}" for k, v in incomplete.items())
            raise ValueError(
                "Upstream task packages are missing required files "
                f"(refusing to generate a partial dataset): {details}"
            )
        if not tasks:
            raise ValueError(f"No task packages found under {tasks_root}.")
        return tasks

    def _select(self, tasks: list[UpstreamTask]) -> list[UpstreamTask]:
        selected = tasks
        if self.task_ids:
            by_key: dict[str, UpstreamTask] = {}
            for task in tasks:
                by_key[task.upstream_id] = task
                by_key[task.local_id] = task
                by_key[task.registry_name] = task
                by_key[f"{REGISTRY_ORG}/{task.upstream_id}"] = task
            unknown = [t for t in self.task_ids if t not in by_key]
            if unknown:
                raise ValueError(f"Unknown CWE-bench task id(s): {', '.join(unknown)}")
            wanted = {by_key[t].local_id for t in self.task_ids}
            selected = [t for t in tasks if t.local_id in wanted]
        if self.limit is not None:
            if self.limit < 0:
                raise ValueError("--limit must not be negative.")
            selected = selected[: self.limit]
        return selected

    # --------------------------------------------------------------- pre-flight

    @staticmethod
    def _link_roots(source_root: Path) -> tuple[Path, ...]:
        """Directories, besides the task itself, that a file link may resolve into.

        Only the Hugging Face cache layout needs one: a snapshot there stores every
        file as a link into the sibling ``blobs/`` directory.
        """
        root = _real(source_root)
        if root.parent.name == "snapshots":
            return (root.parent.parent / "blobs",)
        return ()

    @staticmethod
    def _checked_file(path: Path, home: Path, link_roots: tuple[Path, ...]) -> Path:
        """Return the regular file ``path`` stands for, or refuse it.

        A plain file is returned as is. A link is followed only when it resolves to a
        regular file inside ``home`` (the task package, or the upstream copy for the
        attribution files) or inside a Hugging Face ``blobs/`` directory. Anything
        else, including FIFOs, sockets and device files, is rejected so that host
        files can never be pulled into the dataset.
        """
        mode = os.lstat(path).st_mode
        if stat.S_ISREG(mode):
            return path
        if not stat.S_ISLNK(mode):
            raise ValueError(f"Unsupported file type in the upstream copy: {path}")
        target = _real(path)
        allowed = (_real(home), *link_roots)
        if not any(target.is_relative_to(root) for root in allowed):
            raise ValueError(
                f"Refusing to follow link outside the task package: {path}"
            )
        if not target.is_file():
            raise ValueError(f"Link does not point to a regular file: {path}")
        return target

    def _check_tree(
        self,
        directory: Path,
        home: Path,
        link_roots: tuple[Path, ...],
        depth: int = 0,
    ) -> None:
        """Check that every entry below ``directory`` can be copied later."""
        if depth > _MAX_TREE_DEPTH:
            raise ValueError(f"Directory tree is nested too deeply: {directory}")
        for entry in sorted(directory.iterdir()):
            if entry.is_symlink() and entry.is_dir():
                raise ValueError(f"Directory links are not supported: {entry}")
            if entry.is_dir():
                self._check_tree(entry, home, link_roots, depth + 1)
                continue
            real = self._checked_file(entry, home, link_roots)
            with real.open("rb") as handle:  # opening also proves it is readable
                if (
                    real.stat().st_size < 1024
                    and handle.read(len(_LFS_POINTER_PREFIX)) == _LFS_POINTER_PREFIX
                ):
                    raise ValueError(
                        f"{entry} is a Git LFS pointer, not the file itself. "
                        "Fetch the real files (git lfs pull) or let the adapter "
                        "download the release."
                    )

    def _fingerprint(
        self,
        source_root: Path,
        tasks: list[UpstreamTask],
        link_roots: tuple[Path, ...],
    ) -> str:
        """Identify a local copy by everything the output depends on.

        That is, for every task package the names of its directories and files, the
        content of the files and whether they are executable, plus the attribution
        files.
        """
        digest = hashlib.sha256()

        def add_file(name: bytes, path: Path) -> None:
            content = path.read_bytes()
            kind = b"x" if path.stat().st_mode & 0o111 else b"-"
            digest.update(b"\0".join((name, kind, b"%d" % len(content), b"")))
            digest.update(content)

        for task in tasks:
            tasks_root = task.source_dir.parent
            for current, subdirs, names in os.walk(task.source_dir):
                subdirs.sort()
                digest.update(
                    os.fsencode(os.path.relpath(current, tasks_root)) + b"/\0"
                )
                for name in sorted(names):
                    add_file(os.fsencode(name), _real(Path(current, name)))
        digest.update(b"\0attribution\0")
        for name in ATTRIBUTION_FILES:
            path = source_root / name
            if path.exists():
                add_file(
                    name.encode(), self._checked_file(path, source_root, link_roots)
                )
        return digest.hexdigest()[:16]

    def _check_sources(
        self,
        source_root: Path,
        all_tasks: list[UpstreamTask],
        tasks: list[UpstreamTask],
        link_roots: tuple[Path, ...],
    ) -> None:
        """Check, before anything is written, that the selected tasks can be copied."""
        unverified = self._source_label is None
        for task in all_tasks if unverified else tasks:
            self._check_tree(task.source_dir, task.source_dir, link_roots)
        for task in tasks:
            self._render_task_toml(task, task.source_dir / "task.toml")
        for name in ATTRIBUTION_FILES:
            path = source_root / name
            if path.exists() or path.is_symlink():
                with self._checked_file(path, source_root, link_roots).open("rb"):
                    pass
        if unverified:
            # No release to name: record a fingerprint of the whole copy instead, so
            # that two different copies are never recorded as the same source.
            fingerprint = self._fingerprint(source_root, all_tasks, link_roots)
            self._source_label = f"unverified local copy, sha256 {fingerprint}"
            logger.warning(
                "Using the local copy in %s as is (%d task package(s)). It is not "
                "compared with an upstream release: only required files, links and "
                "Git LFS pointers are checked.",
                source_root,
                len(all_tasks),
            )

    # ---------------------------------------------------------------- the record

    def _profile(self) -> dict[str, str]:
        """What ADAPTER_NOTICE.txt states about every task directory next to it."""
        return {
            _REVISION_FIELD: self._source_label,
            _SOLUTIONS_FIELD: "omitted" if self.omit_solutions else "included",
        }

    def _recorded_profile(self) -> dict[str, str]:
        """The fields of the existing record; empty if it is absent or contradictory."""
        try:
            text = (self.output_dir / PROVENANCE_NAME).read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            return {}
        fields: dict[str, str] = {}
        for field, value in _NOTICE_FIELD_RE.findall(text):
            if fields.setdefault(field, value.strip()) != value.strip():
                return {}
        return fields

    def _notice_text(self) -> str:
        release = next(
            (
                name
                for name, sha in UPSTREAM_RELEASES.items()
                if sha == self._source_label
            ),
            "not a named release",
        )
        lines = [
            "This directory was generated by the cwe-bench adapter of the Harbor adapters",
            f"repository from {HF_REPO_URL}",
            *(f"{field}: {value}" for field, value in self._profile().items()),
            f"Upstream release: {release}",
            "",
            "Changes from upstream: in every task.toml, [task].name was replaced, the",
            "authors of the CWE-bench paper were appended to [task].authors, and",
            "[task].keywords was added where upstream sets none. File modes were",
            "normalized. No other file content was modified.",
        ]
        if self.omit_solutions:
            lines.append(
                "Each task's solution/ directory was replaced by a stub that exits "
                "non-zero."
            )
        return "\n".join(lines) + "\n"

    def _install(self, name: str, write: Callable[[Path], None]) -> None:
        """Put a file at the root of the output directory in one step.

        ``write`` fills a temporary file, which is then renamed into place: an
        interrupted run leaves the earlier file intact, and nothing is written
        through a link that sits at the final path.
        """
        draft = self.output_dir / f".{name}.new"
        try:
            if os.path.lexists(draft):
                draft.unlink()
            write(draft)
            draft.replace(self.output_dir / name)
        except BaseException:
            if os.path.lexists(draft):
                draft.unlink()
            raise

    def _write_notice(self) -> None:
        def write(draft: Path) -> None:
            draft.write_text(self._notice_text(), encoding="utf-8")
            draft.chmod(0o644)

        self._install(PROVENANCE_NAME, write)

    def _write_attribution(
        self, source_root: Path, link_roots: tuple[Path, ...], converting: bool
    ) -> None:
        """Copy LICENSE and NOTICE from the upstream copy into the output directory.

        When the upstream copy lacks one, a file of that name left by the source the
        directory is being converted from is removed; any other is not the adapter's
        to delete.
        """
        for name in ATTRIBUTION_FILES:
            source, target = source_root / name, self.output_dir / name
            if source.exists():
                self._install(
                    name,
                    lambda draft, source=source: self._copy_file(
                        source, draft, source_root, link_roots
                    ),
                )
            elif not os.path.lexists(target):
                logger.warning(
                    "The upstream copy has no %s file; none is written to %s",
                    name,
                    self.output_dir,
                )
            elif converting:
                target.unlink()
                logger.warning(
                    "The upstream copy has no %s file; removed the one the earlier "
                    "source left in %s",
                    name,
                    self.output_dir,
                )
            else:
                logger.warning(
                    "The upstream copy has no %s file; %s does not come from it",
                    name,
                    target,
                )

    def _existing_tasks(self, all_tasks: list[UpstreamTask]) -> list[str]:
        """Names of the entries in the output directory that the record must cover.

        That is every entry holding a task.toml (Harbor takes each for a task,
        whatever its name) and anything else at the path of one of this source's
        tasks, such as what an interrupted removal left behind. The adapter's own
        staging directories never hold a task.toml at their top level.
        """
        task_paths = {task.local_id for task in all_tasks}
        return sorted(
            entry.name
            for entry in os.scandir(self.output_dir)
            if entry.name in task_paths
            or os.path.lexists(os.path.join(entry.path, "task.toml"))
        )

    def _check_output(
        self, all_tasks: list[UpstreamTask], tasks: list[UpstreamTask]
    ) -> bool:
        """Keep the output directory uniform, so that ADAPTER_NOTICE.txt stays true.

        Every task directory in it must come from one upstream source and one
        --omit-solutions setting. Returns True when this run converts a directory
        that was generated differently, which is allowed only when the run
        regenerates every task directory found there.
        """
        selected = {task.local_id for task in tasks}
        if self.omit_solutions:
            # Something at a task path that is not a task may still hold a reference
            # solution; it must be replaced, never skipped.
            for task in all_tasks:
                path = self.output_dir / task.local_id
                if (
                    os.path.lexists(path)
                    and not os.path.lexists(path / "task.toml")
                    and not (self.overwrite and task.local_id in selected)
                ):
                    raise ValueError(
                        f"{path} exists but holds no task.toml, so the adapter cannot "
                        "tell whether it carries a reference solution. Remove it, or "
                        "regenerate it with --overwrite, then run the adapter again."
                    )
        existing = self._existing_tasks(all_tasks)
        if not existing or self._recorded_profile() == self._profile():
            return False
        kept = [name for name in existing if not (self.overwrite and name in selected)]
        if kept:
            raise ValueError(
                f"{self.output_dir} holds task directories that were generated from "
                "another upstream revision or with another --omit-solutions setting, "
                f"or its {PROVENANCE_NAME} is missing or incomplete, and this run "
                f"would leave {len(kept)} of them as they are (for example "
                f"{kept[0]}). Regenerate the whole directory (--overwrite without "
                "--limit or --task-ids, after removing task directories this "
                "release does not contain) or use another directory."
            )
        return True

    # -------------------------------------------------------------- generation

    def _copy_file(
        self, source: Path, target: Path, home: Path, link_roots: tuple[Path, ...]
    ) -> None:
        """Copy one upstream file with a normalized mode (0644, or 0755 if executable).

        Hub downloads carry no meaningful modes, and source modes such as setuid or
        world-writable bits must not leak into a dataset that may be published.
        """
        real = self._checked_file(source, home, link_roots)
        with open(real, "rb", opener=_open_nofollow) as reader:
            mode = os.fstat(reader.fileno()).st_mode
            if not stat.S_ISREG(mode):
                raise ValueError(
                    f"Unsupported file type in the upstream copy: {source}"
                )
            with target.open("wb") as writer:
                shutil.copyfileobj(reader, writer)
        target.chmod(0o755 if mode & 0o111 else 0o644)

    def _copy_tree(
        self,
        source: Path,
        target: Path,
        home: Path,
        link_roots: tuple[Path, ...],
        skip: frozenset[str] = frozenset(),
    ) -> None:
        """Copy a directory; ``skip`` names top-level entries that are left out."""
        target.mkdir()
        target.chmod(0o755)
        for entry in sorted(source.iterdir()):
            if entry.name in skip:
                continue
            if entry.is_dir() and not entry.is_symlink():
                self._copy_tree(entry, target / entry.name, home, link_roots)
            else:
                self._copy_file(entry, target / entry.name, home, link_roots)

    @staticmethod
    def _task_identity(task: UpstreamTask) -> tuple[tomlkit.TOMLDocument, dict]:
        """Render the ``[task]`` identity from the packaged task.toml template."""
        match = _TASK_ID_RE.match(task.upstream_id)
        replacements = {
            "task_id": task.local_id,
            "language": match["language"] if match else "unknown",
            "cwe_id": match["cwe"] if match else "unknown",
        }
        text = (TASK_TEMPLATE_DIR / "task.toml").read_text(encoding="utf-8")
        for key, value in replacements.items():
            text = text.replace(f"{{{key}}}", value)
        plain = tomllib.loads(text).get("task", {})
        missing = [key for key in _IDENTITY_KEYS if key not in plain]
        if missing:
            raise ValueError(
                f"task-template/task.toml lacks [task] field(s): {', '.join(missing)}"
            )
        return tomlkit.parse(text), plain

    def _render_task_toml(self, task: UpstreamTask, task_toml: Path) -> str:
        """Return the upstream task.toml with the registry identity applied.

        Raises if anything other than ``[task].name``, the appended authors and (when
        upstream has none) ``[task].keywords`` would differ from upstream.
        """
        try:
            text = _real(task_toml).read_text(encoding="utf-8")
            before = tomllib.loads(text)
            document = tomlkit.parse(text)
        except (ValueError, RecursionError) as exc:
            raise ValueError(
                f"{task.upstream_id}: task.toml is not valid TOML: {exc}"
            ) from exc
        identity_document, identity = self._task_identity(task)
        upstream_table = before.get("task", {})
        upstream_authors = (
            upstream_table.get("authors", [])
            if isinstance(upstream_table, dict)
            else None
        )
        if not isinstance(upstream_authors, list) or not all(
            isinstance(author, dict) and isinstance(author.get("name"), str)
            for author in upstream_authors
        ):
            raise ValueError(
                f"{task.upstream_id}: [task] in task.toml must be a table whose "
                "authors, if present, is a list of tables with a string name; "
                "refusing to rewrite it."
            )

        expected = copy.deepcopy(before)
        expected_table = expected.setdefault("task", {})
        expected_table["name"] = identity["name"]
        listed = {author["name"] for author in upstream_authors}
        expected_table["authors"] = upstream_authors + [
            author for author in identity["authors"] if author["name"] not in listed
        ]
        if not upstream_table.get("keywords"):
            expected_table["keywords"] = identity["keywords"]

        try:
            if "task" not in document:
                document.add("task", tomlkit.table())
            table = document["task"]
            table["name"] = identity_document["task"]["name"]
            new_authors = [
                author
                for author in identity_document["task"]["authors"]
                if author["name"] not in listed
            ]
            if "authors" not in table:
                table["authors"] = identity_document["task"]["authors"]
            elif isinstance(table["authors"], AoT):
                for author in new_authors:  # upstream wrote [[task.authors]] tables
                    table["authors"].append(author.unwrap())
            elif new_authors:
                for author in new_authors:
                    table["authors"].append(author)
                table["authors"].multiline(True)
            if not upstream_table.get("keywords"):
                table["keywords"] = identity_document["task"]["keywords"]
            rendered = tomlkit.dumps(document)
            unchanged_elsewhere = tomllib.loads(rendered) == expected
        except Exception as exc:  # any tomlkit failure on an unusual layout
            raise ValueError(
                f"{task.upstream_id}: task.toml has a layout the adapter cannot "
                f"rewrite ({type(exc).__name__}: {exc}); refusing to continue."
            ) from exc
        if not unchanged_elsewhere:
            raise ValueError(
                f"{task.upstream_id}: task.toml has a layout the adapter cannot "
                "rewrite without changing other fields; refusing to continue."
            )
        return rendered

    def _generate_task(
        self,
        task: UpstreamTask,
        staging_root: Path,
        link_roots: tuple[Path, ...],
    ) -> bool:
        output_dir = self.output_dir / task.local_id
        if os.path.lexists(output_dir) and not self.overwrite:
            if (output_dir / "task.toml").is_file():
                logger.info("Skipping %s (exists; use --overwrite)", task.local_id)
            else:
                logger.warning(
                    "Skipping %s: %s exists but holds no task.toml; use --overwrite "
                    "to replace it",
                    task.local_id,
                    output_dir,
                )
            return False

        # Build the task in the staging directory and move it into place with a
        # rename only once it is complete, so an interrupted run never leaves a
        # half-written task directory. With --omit-solutions the upstream solution/
        # is never copied into the output directory or its staging area (the
        # upstream copy the adapter reads from still holds it).
        staging = staging_root / task.local_id
        skip = frozenset({"solution"}) if self.omit_solutions else frozenset()
        self._copy_tree(task.source_dir, staging, task.source_dir, link_roots, skip)
        (staging / "task.toml").write_text(
            self._render_task_toml(task, task.source_dir / "task.toml"),
            encoding="utf-8",
        )
        (staging / "task.toml").chmod(0o644)
        if self.omit_solutions:
            (staging / "solution").mkdir()
            (staging / "solution").chmod(0o755)
            shutil.copyfile(
                TASK_TEMPLATE_DIR / "solution" / "solve.sh",
                staging / "solution" / "solve.sh",
            )
        # Hub downloads do not carry the executable bit; Harbor also sets it at run
        # time, this just keeps the generated tree consistent on disk.
        for script in ("solution/solve.sh", "tests/test.sh"):
            (staging / script).chmod(0o755)

        # Replace an existing copy by parking it, so that it can be put back if the
        # new one cannot be moved in.
        previous = staging_root / f"{task.local_id}.previous"
        try:
            if os.path.lexists(output_dir):
                try:
                    output_dir.rename(previous)
                except OSError as exc:
                    if exc.errno != errno.EXDEV:
                        raise
                    # A directory from a lower overlayfs layer cannot be renamed. It
                    # is deleted in place, task.toml first, so that an interrupted
                    # removal does not leave something that passes for a task.
                    self._remove(output_dir / "task.toml")
                    self._remove(output_dir)
            staging.rename(output_dir)
        except BaseException:
            if os.path.lexists(previous) and not os.path.lexists(output_dir):
                previous.rename(output_dir)
            raise
        self._remove(previous)
        return True

    @staticmethod
    def _remove(path: Path) -> None:
        """Delete a parked or staged entry; a run must not succeed while it exists."""
        try:
            if path.is_symlink() or not path.is_dir():
                path.unlink(missing_ok=True)
            else:
                shutil.rmtree(path)
        except OSError as exc:
            raise OSError(
                f"Could not remove {path} ({exc}). It may hold an earlier copy of a "
                "task, including its reference solution. Make the output directory "
                "writable (chmod -R u+rwX) or delete that entry by hand, then run the "
                "adapter again."
            ) from exc

    def _lock_output_dir(self) -> tuple[int, bool]:
        """Take an exclusive lock on the output directory for the rest of the run.

        Returns the open descriptor and whether the lock is held (some file systems
        do not support locks).
        """
        descriptor = os.open(self.output_dir, os.O_RDONLY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            raise ValueError(
                f"Another cwe-bench run is writing to {self.output_dir}; wait for it "
                "to finish and run the adapter again."
            ) from None
        except OSError as exc:
            logger.warning(
                "Could not lock %s (%s); make sure no other cwe-bench run writes to "
                "it at the same time.",
                self.output_dir,
                exc,
            )
            return descriptor, False
        return descriptor, True

    def _clear_leftovers(self, locked: bool) -> None:
        """Remove staging directories that interrupted runs left behind.

        Only the holder of the output directory's lock can tell that such a directory
        is not in use. Without the lock the run stops instead, because a leftover may
        hold task copies made with other settings.
        """
        leftovers = [
            Path(entry.path)
            for entry in os.scandir(self.output_dir)
            if _STAGING_NAME_RE.fullmatch(entry.name)
            and entry.is_dir(follow_symlinks=False)
        ]
        if leftovers and not locked:
            raise ValueError(
                f"{leftovers[0]} was left by another cwe-bench run, and without a "
                "lock the adapter cannot tell whether that run is still active. "
                f"Delete it once no other run is writing to {self.output_dir}, then "
                "run the adapter again."
            )
        for leftover in leftovers:
            self._remove(leftover)

    def _staging_root(self) -> Path:
        """Create this run's private staging directory inside the output directory.

        Tasks are built one level below it: Harbor's run and publish commands only
        look at the immediate sub-directories of a dataset, and being on the file
        system of their final place lets the tasks move in with a rename.
        """
        root = self.output_dir / f"{_STAGING_PREFIX}{secrets.token_hex(4)}"
        root.mkdir()
        root.chmod(0o700)
        return root

    def run(self) -> None:
        """Fetch the pinned CWE-bench release and write Harbor task directories."""
        self._source_label = None
        if self.source_dir is None:
            source_root = self._download_snapshot()
        else:
            source_root = self._local_copy()
        link_roots = self._link_roots(source_root)
        all_tasks = self.discover_tasks(source_root)
        tasks = self._select(all_tasks)
        self._check_sources(source_root, all_tasks, tasks, link_roots)

        self.output_dir.mkdir(parents=True, exist_ok=True)
        lock, locked = self._lock_output_dir()
        staging_root = None
        try:
            self._clear_leftovers(locked)
            converting = self._check_output(all_tasks, tasks)
            staging_root = self._staging_root()
            if converting:
                # Until every task has been regenerated the directory matches
                # neither the old record nor the new one.
                (self.output_dir / PROVENANCE_NAME).unlink(missing_ok=True)
            else:
                self._write_notice()
            self._write_attribution(source_root, link_roots, converting)
            written = sum(
                self._generate_task(task, staging_root, link_roots) for task in tasks
            )
            # The record is written only once nothing of an earlier copy is left.
            self._remove(staging_root)
            self._write_notice()
        finally:
            if staging_root is not None:
                shutil.rmtree(staging_root, ignore_errors=True)
            os.close(lock)

        logger.info(
            "Wrote %d of %d selected task(s) to %s%s",
            written,
            len(tasks),
            self.output_dir,
            " (reference solutions omitted)" if self.omit_solutions else "",
        )


_ACCESS_HELP = f"""\
Could not read the gated dataset {HF_REPO_ID}.

CWE-bench is gated on Hugging Face (access is granted automatically). To use this
adapter:
  1. Sign in at https://huggingface.co and open {HF_REPO_URL}
  2. Review and accept the access conditions shown on that page.
  3. Authenticate on this machine: `hf auth login`, or export HF_TOKEN with a token
     that can read gated repositories.
Then re-run the adapter. To use a complete copy you already downloaded, pass
--source-dir."""
