"""Input data handling for LatchBio evaluations.

Every evaluation points at its inputs with Latch Data URIs
(``latch://<id>.node``). The upstream harness downloads each node with the
Latch SDK and mounts it at ``/workspace/data/<node name>``; a node is either a
single file or a directory tree.

Reading Latch Data requires a (free) Latch account, so the adapter separates
*pinning* the data from *obtaining* it:

``data_manifest.json`` (committed next to this module)
    Records, for every node used by an adapted evaluation, its name and the
    path, size and SHA-256 of each file. It also names the public mirror the
    files can be downloaded from without Latch credentials.

``--data-source mirror`` (default)
    Tasks download their files from the mirror at image build time and verify
    them against the manifest. No credentials are needed.

``--data-source latch``
    The adapter downloads the nodes with the Latch SDK (``latch login``
    required) into a local cache and copies them into each task's build
    context. This is how the manifest and the mirror are produced, and it lets
    anyone regenerate the dataset straight from the upstream source.

``--data-source none``
    No data is staged. Only useful to exercise the oracle and verifier, which
    never read the input data.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import quote

MANIFEST_PATH = Path(__file__).parent / "data_manifest.json"
DATA_SOURCES = ("mirror", "latch", "none")
SPLITS = ("full", "small-data")

# Small-data subset rule: evaluations whose input data totals at most 2 GiB.
# It is for hosts that cannot give a container upstream's 32 GB: the largest
# inputs (single AnnData files of 3-9 GB, 7 GB genotype archives) do not fit in
# a few GiB of memory, so those evaluations are outside the subset. The rule
# depends only on the pinned manifest, which makes the subset reproducible. The
# reduced-resource replication of the parity experiment ran on this subset.
SMALL_DATA_MAX_BYTES = 2 * 1024**3
CONTAINER_DATA_DIR = "/workspace/data"

_NODE_URI = re.compile(r"^latch://(\d+)\.node/?$")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_CHUNK = 8 * 1024 * 1024
_COMPLETE_MARKER = ".latchbio-complete"


class DataError(RuntimeError):
    """Raised when evaluation data cannot be pinned or staged."""


def default_data_dir() -> Path:
    """Local cache for data downloaded from Latch."""
    override = os.environ.get("LATCHBIO_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cache" / "harbor-latchbio" / "data"


def node_key(uri: str) -> str:
    """Stable directory / mirror key for a Latch Data URI."""
    match = _NODE_URI.match(uri)
    if match:
        return match.group(1)
    return _UNSAFE.sub("_", uri.removeprefix("latch://")).strip("_")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class DataFile:
    path: str
    """POSIX path relative to ``/workspace/data``; starts with the node name."""
    size: int
    sha256: str

    def to_json(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


@dataclass(frozen=True)
class DataNode:
    uri: str
    name: str
    """Name the upstream harness mounts the node under (``LPath(uri).name()``)."""
    kind: str
    """``file`` or ``dir``."""
    files: tuple[DataFile, ...]

    @property
    def size(self) -> int:
        return sum(item.size for item in self.files)

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "files": [item.to_json() for item in self.files],
        }

    @classmethod
    def from_json(cls, uri: str, data: dict[str, Any]) -> "DataNode":
        return cls(
            uri=uri,
            name=data["name"],
            kind=data["kind"],
            files=tuple(
                DataFile(path=item["path"], size=item["size"], sha256=item["sha256"])
                for item in data["files"]
            ),
        )


@dataclass
class DataManifest:
    """Pinned description of every Latch Data node the adapter uses."""

    nodes: dict[str, DataNode] = field(default_factory=dict)
    mirror_base_url: str | None = None
    mirror_notes: str | None = None

    @classmethod
    def load(cls, path: Path = MANIFEST_PATH) -> "DataManifest":
        if not path.is_file():
            return cls()
        raw = json.loads(path.read_text())
        mirror = raw.get("mirror") or {}
        return cls(
            nodes={
                uri: DataNode.from_json(uri, node)
                for uri, node in (raw.get("nodes") or {}).items()
            },
            mirror_base_url=mirror.get("base_url"),
            mirror_notes=mirror.get("notes"),
        )

    def save(self, path: Path = MANIFEST_PATH) -> None:
        payload = {
            "schema_version": 1,
            "description": (
                "Files behind the Latch Data nodes used by the adapted LatchBio "
                "evaluations. Paths are relative to /workspace/data."
            ),
            "mirror": {"base_url": self.mirror_base_url, "notes": self.mirror_notes},
            "nodes": {uri: self.nodes[uri].to_json() for uri in sorted(self.nodes)},
        }
        path.write_text(json.dumps(payload, indent=2) + "\n")

    def file_url(self, node: DataNode, item: DataFile) -> str:
        if not self.mirror_base_url:
            raise DataError("data_manifest.json does not define a mirror base_url")
        return (
            f"{self.mirror_base_url.rstrip('/')}/{node_key(node.uri)}/"
            f"{quote(item.path, safe='/')}"
        )


def scan_node(uri: str, name: str, root: Path) -> DataNode:
    """Describe a downloaded node rooted at ``root`` (a file or a directory)."""
    if root.is_file():
        return DataNode(
            uri=uri,
            name=name,
            kind="file",
            files=(DataFile(name, root.stat().st_size, sha256_of(root)),),
        )
    if not root.is_dir():
        raise DataError(f"Downloaded node {uri} not found at {root}")
    files = tuple(
        DataFile(
            (PurePosixPath(name) / path.relative_to(root).as_posix()).as_posix(),
            path.stat().st_size,
            sha256_of(path),
        )
        for path in sorted(p for p in root.rglob("*") if p.is_file())
    )
    return DataNode(uri=uri, name=name, kind="dir", files=files)


def download_node(uri: str, data_dir: Path) -> DataNode:
    """Download one node with the Latch SDK into ``data_dir`` and describe it.

    Mirrors ``latch_eval_tools.harness.utils.download_single_dataset``: the node
    is fetched with ``LPath(uri).download`` and keeps the name Latch reports for
    it. Nodes are cached under ``<data_dir>/<node key>/<name>``.
    """
    try:
        from latch.ldata.path import LPath
    except ImportError as exc:
        raise DataError(
            "--data-source latch needs the Latch SDK. Install the optional "
            "dependency (`uv sync --extra latch`) and run `latch login`."
        ) from exc

    node_dir = data_dir / node_key(uri)
    marker = node_dir / _COMPLETE_MARKER
    try:
        remote = LPath(uri)
        name = remote.name() or Path(uri).name or "data"
        target = node_dir / name
        if not marker.is_file():
            if node_dir.exists():
                shutil.rmtree(node_dir)
            node_dir.mkdir(parents=True)
            print(f"  downloading {uri} -> {target}", flush=True)
            remote.download(dst=target, cache=True)
            marker.write_text(name + "\n")
    except Exception as exc:  # noqa: BLE001 -- surface SDK/auth errors with context
        raise DataError(
            f"Could not download {uri} from Latch: {type(exc).__name__}: {exc}. "
            "Make sure `latch login` succeeded and the account can read the node."
        ) from exc
    return scan_node(uri, name, target)


def resolve_nodes(nodes: Iterable[DataNode]) -> list[DataNode]:
    """Apply upstream's mount rule to an eval's nodes.

    ``download_data`` stages nodes in order under their name and replaces any
    earlier node that has the same name, so the last one wins.
    """
    by_name: dict[str, DataNode] = {}
    for node in nodes:
        by_name.pop(node.name, None)
        by_name[node.name] = node
    return list(by_name.values())


def task_manifest(
    nodes: list[DataNode], manifest: DataManifest, *, with_urls: bool
) -> dict[str, Any]:
    """Per-task ``environment/data_manifest.json`` consumed by ``fetch_data.py``."""
    files = []
    for node in nodes:
        for item in node.files:
            entry = item.to_json()
            if with_urls:
                entry["url"] = manifest.file_url(node, item)
            files.append(entry)
    return {
        "nodes": [{"uri": node.uri, "name": node.name, "kind": node.kind} for node in nodes],
        "files": files,
    }


def stage_local_files(nodes: list[DataNode], data_dir: Path, dest: Path) -> None:
    """Copy cached node files into a task's build context (``environment/data``)."""
    for node in nodes:
        for item in node.files:
            source = data_dir / node_key(node.uri) / item.path
            if not source.is_file():
                raise DataError(f"Cached file missing for {node.uri}: {source}")
            target = dest / item.path
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                target.unlink()
            try:
                os.link(source, target)
            except OSError:
                shutil.copy2(source, target)


_FETCH_STAGE = """\
COPY data_manifest.json fetch_data.py /opt/latchbio/
RUN python3 /opt/latchbio/fetch_data.py \\
        --manifest /opt/latchbio/data_manifest.json \\
        --dest /workspace/data \\
    && rm -rf /opt/latchbio"""

DOCKERFILE_DATA_STAGES = {
    "mirror": "# Downloaded from the public mirror and verified against pinned SHA-256 sums.\n"
    + _FETCH_STAGE,
    "latch": "# Copied from the build context (downloaded from Latch at generation time)\n"
    "# and verified against pinned SHA-256 sums.\n"
    "COPY data/ /workspace/data/\n" + _FETCH_STAGE,
    "none": "# Generated with --data-source none: no input data is staged. The oracle and\n"
    "# verifier work, but agents cannot solve the task in this image.\n"
    "RUN mkdir -p /workspace/data",
}
