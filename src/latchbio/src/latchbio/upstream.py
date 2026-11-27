"""Load LatchBio evaluation definitions from pinned upstream repositories."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .sources import ORG, BenchmarkSource

_SAFE_ID = re.compile(r"[^a-z0-9_-]+")


class UpstreamError(RuntimeError):
    """Raised when an upstream repository cannot be read at its pinned commit."""


def sanitize_id(value: str) -> str:
    """Lowercase an upstream identifier and make it registry-safe.

    Upstream eval ids are already ``[A-Za-z0-9_]``; anything else becomes a
    hyphen and leading/trailing separators are stripped.
    """
    cleaned = _SAFE_ID.sub("-", value.strip().lower()).strip("-_")
    if not cleaned:
        raise ValueError(f"Cannot derive a task id from upstream id {value!r}")
    return cleaned


@dataclass(frozen=True)
class UpstreamEval:
    """One public evaluation, as published in an upstream repository."""

    source: BenchmarkSource
    path: str
    """Repository-relative POSIX path of the eval JSON file."""
    raw: dict[str, Any]

    @property
    def eval_id(self) -> str:
        return self.raw["id"]

    @property
    def task_id(self) -> str:
        """Harbor task directory name: ``<benchmark>__<eval-id>``."""
        return f"{self.source.key}__{sanitize_id(self.eval_id)}"

    @property
    def task_name(self) -> str:
        """Registry name: ``latchbio/<benchmark>__<eval-id>``."""
        return f"{ORG}/{self.task_id}"

    @property
    def graders(self) -> list[dict[str, Any]]:
        """The eval's grader specs: one for ``grader``, several for ``graders``."""
        if self.raw.get("grader") is not None:
            return [self.raw["grader"]]
        return list(self.raw.get("graders") or [])

    @property
    def data_nodes(self) -> list[str]:
        nodes = self.raw.get("data_node")
        if not nodes:
            return []
        return [nodes] if isinstance(nodes, str) else list(nodes)

    @property
    def metadata(self) -> dict[str, Any]:
        return self.raw.get("metadata") or {}


def _git(*args: str, cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        )
    except FileNotFoundError as exc:
        raise UpstreamError("git is required to fetch the upstream benchmarks") from exc
    except subprocess.CalledProcessError as exc:
        raise UpstreamError(
            f"git {' '.join(args)} failed: {exc.stderr.strip() or exc.stdout.strip()}"
        ) from exc
    return result.stdout.strip()


def repo_dir_name(source: BenchmarkSource) -> str:
    return source.repo.rstrip("/").rsplit("/", 1)[-1]


def ensure_checkout(
    source: BenchmarkSource,
    *,
    cache_dir: Path,
    upstream_root: Path | None = None,
) -> Path:
    """Return a working tree of ``source`` at its pinned commit.

    With ``upstream_root``, an existing clone at ``<upstream_root>/<repo>`` is
    used and must already be at the pinned commit. Otherwise the eval directory
    is fetched into ``cache_dir`` as a sparse, blob-filtered checkout, which
    avoids downloading the (large) published trajectories.
    """
    if upstream_root is not None:
        checkout = upstream_root / repo_dir_name(source)
        if not (checkout / ".git").exists():
            raise UpstreamError(
                f"--upstream-root has no git checkout of {source.repo} at {checkout}"
            )
        head = _git("rev-parse", "HEAD", cwd=checkout)
        if head != source.commit:
            raise UpstreamError(
                f"{checkout} is at {head}, but the adapter pins {source.display_name} "
                f"to {source.commit}. Check out the pinned commit or drop "
                "--upstream-root to let the adapter fetch it."
            )
        return checkout

    checkout = cache_dir / f"{source.key}-{source.commit[:12]}"
    if checkout.exists():
        try:
            head = _git("rev-parse", "HEAD", cwd=checkout)
        except UpstreamError:
            head = None
        if head == source.commit:
            return checkout
        # Left over from an interrupted or foreign checkout: start again.
        shutil.rmtree(checkout)

    # Fetch into a scratch directory and move it into place only once it is
    # complete, so an interrupted fetch never leaves a half-built cache entry.
    cache_dir.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=f"{checkout.name}.", dir=cache_dir))
    try:
        _git("init", "--quiet", cwd=scratch)
        _git("remote", "add", "origin", source.repo, cwd=scratch)
        _git("sparse-checkout", "set", "--no-cone", f"/{source.evals_dir}/", cwd=scratch)
        _git(
            "fetch",
            "--quiet",
            "--depth",
            "1",
            "--filter=blob:none",
            "origin",
            source.commit,
            cwd=scratch,
        )
        _git("checkout", "--quiet", "--detach", "FETCH_HEAD", cwd=scratch)
        head = _git("rev-parse", "HEAD", cwd=scratch)
        if head != source.commit:
            raise UpstreamError(f"Fetched {head} for {source.repo}, expected {source.commit}")
        scratch.rename(checkout)
    finally:
        # A no-op after a successful rename; removes the scratch tree otherwise.
        shutil.rmtree(scratch, ignore_errors=True)
    return checkout


def load_evals(source: BenchmarkSource, checkout: Path) -> list[UpstreamEval]:
    """Load every adaptable public eval of ``source``, sorted by repository path.

    An eval is adaptable when it ships a grader (``grader`` or ``graders``) and
    its input data pointers (``data_node``). Benchmarks whose public release
    lacks either are not listed in ``sources.BENCHMARKS`` at all, so a missing
    field here means upstream changed and is reported rather than skipped.
    """
    evals_root = checkout / source.evals_dir
    if not evals_root.is_dir():
        raise UpstreamError(f"{source.repo} has no {source.evals_dir}/ at {source.commit}")

    evals: list[UpstreamEval] = []
    for path in sorted(evals_root.rglob("*.json")):
        if path.name in source.non_eval_files:
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        rel = path.relative_to(checkout).as_posix()
        if not isinstance(raw, dict) or "id" not in raw or "task" not in raw:
            raise UpstreamError(f"{source.repo}:{rel} is not an eval definition")
        if raw["id"] in source.excluded_eval_ids:
            continue
        item = UpstreamEval(source=source, path=rel, raw=raw)
        if (raw.get("grader") is None) == (raw.get("graders") is None):
            raise UpstreamError(
                f"{source.repo}:{rel} must define exactly one of 'grader' or 'graders'"
            )
        if not item.data_nodes:
            raise UpstreamError(f"{source.repo}:{rel} has no data_node")
        evals.append(item)

    task_ids = [item.task_id for item in evals]
    duplicates = sorted({tid for tid in task_ids if task_ids.count(tid) > 1})
    if duplicates:
        raise UpstreamError(f"Task ids are not unique for {source.key}: {duplicates}")
    return evals
