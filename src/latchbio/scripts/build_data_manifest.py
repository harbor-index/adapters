#!/usr/bin/env python3
"""Pin (and optionally mirror) the Latch Data nodes used by the adapted evaluations.

Downloads every ``data_node`` referenced by an adapted eval with the Latch SDK,
hashes the files, and records them in ``src/latchbio/data_manifest.json``. The
manifest is what makes the dataset reproducible: every task verifies its input
files against it at image build time, whichever source the bytes come from.

Maintainers run this once per upstream revision. It needs a Latch account
(``latch login``, or an API token from https://console.latch.bio/settings/developer
saved to ``~/.latch/token``):

    uv sync --extra latch
    uv run --extra latch python scripts/build_data_manifest.py

With ``--hf-repo``, each node is also uploaded to a Hugging Face dataset repo
laid out as ``<node id>/<path>``, which is the layout ``--data-source mirror``
tasks download from (needs ``huggingface_hub`` and a write token):

    uv run --extra latch --with huggingface_hub \\
        python scripts/build_data_manifest.py --hf-repo <user>/<dataset>

Nodes are processed smallest first and the manifest is saved after each one, so
an interrupted run resumes where it stopped. ``--prune-larger-than`` deletes the
local copy of big nodes once they are pinned (and uploaded), which keeps the
peak disk usage near the size of the largest node instead of the whole dataset.

``--check`` downloads nothing: it verifies that the cached files still match
the committed manifest and exits non-zero on any difference.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ADAPTER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ADAPTER_ROOT / "src"))

from latchbio.adapter import LatchBioAdapter  # noqa: E402
from latchbio.data import (  # noqa: E402
    MANIFEST_PATH,
    DataError,
    DataManifest,
    DataNode,
    default_data_dir,
    download_node,
    node_key,
    scan_node,
)

HF_URL = "https://huggingface.co/datasets/{repo}/resolve/{revision}"


def human(size: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def remote_size(uri: str) -> int:
    from latch.ldata.path import LPath

    return int(LPath(uri).size_recursive())


def uploaded_paths(api, repo: str) -> set[str]:
    return set(api.list_repo_files(repo, repo_type="dataset"))


def upload_node(api, repo: str, node: DataNode, data_dir: Path, existing: set[str]) -> int:
    """Upload a node's files to ``<node id>/<path>``; returns how many were sent."""
    key = node_key(node.uri)
    sent = 0
    for item in node.files:
        target = f"{key}/{item.path}"
        if target in existing:
            continue
        api.upload_file(
            path_or_fileobj=str(data_dir / key / item.path),
            path_in_repo=target,
            repo_id=repo,
            repo_type="dataset",
            commit_message=f"Add {target}",
        )
        existing.add(target)
        sent += 1
    return sent


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--data-dir", type=Path, default=default_data_dir())
    parser.add_argument("--upstream-root", type=Path, default=None)
    parser.add_argument("--benchmarks", nargs="+", default=None)
    parser.add_argument(
        "--max-node-bytes",
        type=int,
        default=None,
        help="Skip nodes larger than this (they stay unpinned until a later run)",
    )
    parser.add_argument(
        "--prune-larger-than",
        type=int,
        default=None,
        help="Delete the local copy of nodes larger than this once pinned/uploaded",
    )
    parser.add_argument("--hf-repo", default=None, help="Mirror to this Hugging Face dataset repo")
    parser.add_argument(
        "--mirror-revision",
        default="main",
        help="Revision recorded in the mirror URL; pass the final commit SHA to pin it",
    )
    parser.add_argument("--mirror-notes", default=None)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    adapter = LatchBioAdapter(
        Path("unused"),
        benchmarks=args.benchmarks,
        data_source="none",
        upstream_root=args.upstream_root,
    )
    evals = adapter.load_all_evals()
    uris = sorted({uri for item in evals for uri in item.data_nodes})
    print(f"{len(evals)} evals reference {len(uris)} Latch Data nodes")

    manifest = DataManifest.load()

    if args.check:
        problems = []
        for uri in uris:
            pinned = manifest.nodes.get(uri)
            if pinned is None:
                problems.append(f"{uri}: not in the committed manifest")
                continue
            root = args.data_dir / node_key(uri) / pinned.name
            if not root.exists():
                print(f"skip  {uri} ({pinned.name}): not cached locally")
                continue
            if scan_node(uri, pinned.name, root).to_json() != pinned.to_json():
                problems.append(f"{uri}: cached files differ from the manifest")
            else:
                print(f"ok    {uri} ({pinned.name})")
        for problem in problems:
            print(f"FAIL: {problem}", file=sys.stderr)
        return 1 if problems else 0

    api = existing = None
    if args.hf_repo:
        from huggingface_hub import HfApi

        api = HfApi()
        api.create_repo(args.hf_repo, repo_type="dataset", exist_ok=True)
        existing = uploaded_paths(api, args.hf_repo)
        manifest.mirror_base_url = HF_URL.format(repo=args.hf_repo, revision=args.mirror_revision)
        if args.mirror_notes:
            manifest.mirror_notes = args.mirror_notes

    sizes = {uri: remote_size(uri) for uri in uris}
    order = sorted(uris, key=lambda uri: (sizes[uri], uri))
    print(f"Total size on Latch: {human(sum(sizes.values()))}")

    failures: list[str] = []
    for index, uri in enumerate(order, 1):
        label = f"[{index}/{len(order)}] {uri} ({human(sizes[uri])})"
        if args.max_node_bytes is not None and sizes[uri] > args.max_node_bytes:
            print(f"{label}: skipped (larger than --max-node-bytes)")
            continue

        pinned = manifest.nodes.get(uri)
        key = node_key(uri)
        mirrored = existing is not None and pinned is not None and all(
            f"{key}/{item.path}" in existing for item in pinned.files
        )
        if pinned is not None and (existing is None or mirrored):
            print(f"{label}: already pinned{' and mirrored' if mirrored else ''} ({pinned.name})")
            continue

        try:
            node = download_node(uri, args.data_dir)
            if pinned is not None and pinned.to_json() != node.to_json():
                print(f"  NOTE: {uri} changed since it was pinned; updating the manifest")
            manifest.nodes[uri] = node
            sent = upload_node(api, args.hf_repo, node, args.data_dir, existing) if api else 0
            manifest.save(MANIFEST_PATH)
        except Exception as exc:  # noqa: BLE001 -- keep going; report at the end
            failures.append(f"{uri}: {type(exc).__name__}: {exc}")
            print(f"{label}: FAILED ({type(exc).__name__}: {exc})")
            continue

        note = f", uploaded {sent} file(s)" if api else ""
        print(f"{label}: {node.name} ({node.kind}, {len(node.files)} file(s)){note}")
        if args.prune_larger_than is not None and node.size > args.prune_larger_than:
            shutil.rmtree(args.data_dir / key, ignore_errors=True)
            print(f"  pruned local copy of {uri}")

    manifest.save(MANIFEST_PATH)
    pinned_total = sum(manifest.nodes[uri].size for uri in uris if uri in manifest.nodes)
    missing = [uri for uri in uris if uri not in manifest.nodes]
    print(
        f"\n{MANIFEST_PATH}: {len(uris) - len(missing)}/{len(uris)} nodes pinned "
        f"({human(pinned_total)}); mirror: {manifest.mirror_base_url}"
    )
    for failure in failures:
        print(f"FAIL: {failure}", file=sys.stderr)
    if missing:
        print(f"Not pinned yet: {len(missing)} node(s)", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DataError as exc:
        sys.exit(f"error: {exc}")
