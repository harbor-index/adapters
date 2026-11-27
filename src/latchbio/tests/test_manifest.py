"""The committed data manifest must pin every input of every adapted eval."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from latchbio.adapter import LatchBioAdapter, default_cache_dir
from latchbio.data import SMALL_DATA_MAX_BYTES, DataManifest
from latchbio.sources import BENCHMARKS

MANIFEST = DataManifest.load()


def test_manifest_is_present_and_names_an_immutable_mirror_revision():
    assert MANIFEST.nodes, "data_manifest.json is missing or empty"
    assert MANIFEST.mirror_base_url
    # .../resolve/<40-hex commit>: a branch name would let the files change.
    assert re.search(r"/resolve/[0-9a-f]{40}$", MANIFEST.mirror_base_url)


def test_every_pinned_file_has_a_size_and_checksum():
    for uri, node in MANIFEST.nodes.items():
        assert re.fullmatch(r"latch://\d+\.node", uri), uri
        assert node.kind in {"file", "dir"} and node.files, uri
        seen = set()
        for item in node.files:
            assert re.fullmatch(r"[0-9a-f]{64}", item.sha256), (uri, item.path)
            assert item.size >= 0
            assert item.path == node.name or item.path.startswith(node.name + "/"), (uri, item.path)
            assert ".." not in Path(item.path).parts and not item.path.startswith("/")
            assert item.path not in seen
            seen.add(item.path)


def _cached_upstream() -> bool:
    cache = default_cache_dir()
    return all((cache / f"{s.key}-{s.commit[:12]}" / ".git").exists() for s in BENCHMARKS)


@pytest.mark.skipif(not _cached_upstream(), reason="upstream repositories are not cached locally")
def test_manifest_covers_every_adapted_eval_and_defines_the_splits():
    adapter = LatchBioAdapter(Path("unused"), data_source="none")
    evals = adapter.load_all_evals()
    assert len(evals) == 44

    referenced = {uri for item in evals for uri in item.data_nodes}
    assert referenced == set(MANIFEST.nodes), "manifest and eval data nodes differ"

    small = [item for item in evals if adapter.in_small_data_split(item)]
    assert len(small) == 38
    excluded = sorted(item.task_id for item in evals if not adapter.in_small_data_split(item))
    assert excluded == [
        "spatialbench__dr01_batch_confounded_clustering",
        "spatialbench__norm01_batch_correction",
        "spatialbench__spatial07_sex_housekeeping_de",
        "spatialbench__spatial10_genome_wide_de_pct",
        "variantbench__cis_mr_bcaa",
        "variantbench__small_variant_calling_mnv_codon_consequence",
    ]
    assert all(adapter.data_size(item) > SMALL_DATA_MAX_BYTES for item in evals if item.task_id in excluded)

    # Upstream mounts each node under its name; two nodes of one eval must not collide.
    for item in evals:
        names = [MANIFEST.nodes[uri].name for uri in item.data_nodes]
        assert len(names) == len(set(names)), item.task_id
