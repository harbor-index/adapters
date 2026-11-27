"""Data pinning, mirror URLs and build-context staging."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from latchbio.adapter import TEMPLATE_DIR
from latchbio.data import (
    DOCKERFILE_DATA_STAGES,
    DataFile,
    DataManifest,
    DataNode,
    node_key,
    resolve_nodes,
    scan_node,
    stage_local_files,
    task_manifest,
)

FETCH = TEMPLATE_DIR / "environment" / "fetch_data.py"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_node_key_uses_the_numeric_latch_node_id():
    assert node_key("latch://203298116.node") == "203298116"
    assert node_key("latch://38438.account/path/to/x.h5ad") == "38438.account_path_to_x.h5ad"


def test_scan_node_describes_files_and_directories(tmp_path):
    single = tmp_path / "counts.h5ad"
    single.write_bytes(b"abc")
    node = scan_node("latch://1.node", "counts.h5ad", single)
    assert node.kind == "file"
    assert node.files == (DataFile("counts.h5ad", 3, _sha(b"abc")),)

    tree = tmp_path / "run"
    (tree / "sub").mkdir(parents=True)
    (tree / "b.txt").write_bytes(b"b")
    (tree / "sub" / "a.txt").write_bytes(b"aa")
    node = scan_node("latch://2.node", "run", tree)
    assert node.kind == "dir"
    assert [item.path for item in node.files] == ["run/b.txt", "run/sub/a.txt"]
    assert node.size == 3


def test_manifest_round_trips_and_builds_mirror_urls(tmp_path):
    node = DataNode("latch://7.node", "my data.h5ad", "file", (DataFile("my data.h5ad", 3, "f" * 64),))
    manifest = DataManifest(nodes={node.uri: node}, mirror_base_url="https://mirror.example/data/")
    path = tmp_path / "data_manifest.json"
    manifest.save(path)
    loaded = DataManifest.load(path)
    assert loaded.nodes == {node.uri: node}
    assert loaded.file_url(node, node.files[0]) == "https://mirror.example/data/7/my%20data.h5ad"


def test_missing_manifest_loads_as_empty(tmp_path):
    manifest = DataManifest.load(tmp_path / "absent.json")
    assert manifest.nodes == {} and manifest.mirror_base_url is None


def test_later_node_with_the_same_name_replaces_the_earlier_one():
    # Upstream's download_data re-stages a name each time it recurs.
    first = DataNode("latch://1.node", "data.csv", "file", (DataFile("data.csv", 1, "a" * 64),))
    other = DataNode("latch://2.node", "other.csv", "file", (DataFile("other.csv", 1, "b" * 64),))
    second = DataNode("latch://3.node", "data.csv", "file", (DataFile("data.csv", 2, "c" * 64),))
    assert resolve_nodes([first, other, second]) == [other, second]


def test_task_manifest_lists_urls_only_for_the_mirror():
    node = DataNode("latch://7.node", "x.csv", "file", (DataFile("x.csv", 3, "f" * 64),))
    manifest = DataManifest(nodes={node.uri: node}, mirror_base_url="https://mirror.example")
    with_urls = task_manifest([node], manifest, with_urls=True)
    assert with_urls["files"] == [
        {"path": "x.csv", "size": 3, "sha256": "f" * 64, "url": "https://mirror.example/7/x.csv"}
    ]
    assert "url" not in task_manifest([node], manifest, with_urls=False)["files"][0]


def test_stage_local_files_copies_cached_nodes_into_the_build_context(tmp_path):
    cache = tmp_path / "cache"
    (cache / "9" / "run" / "sub").mkdir(parents=True)
    (cache / "9" / "run" / "sub" / "a.txt").write_bytes(b"aa")
    node = scan_node("latch://9.node", "run", cache / "9" / "run")
    stage_local_files([node], cache, tmp_path / "ctx")
    assert (tmp_path / "ctx" / "run" / "sub" / "a.txt").read_bytes() == b"aa"


def test_every_data_source_has_a_dockerfile_stage():
    assert set(DOCKERFILE_DATA_STAGES) == {"mirror", "latch", "none"}
    assert "COPY data/ /workspace/data/" in DOCKERFILE_DATA_STAGES["latch"]
    assert "COPY data/" not in DOCKERFILE_DATA_STAGES["mirror"]
    for mode in ("mirror", "latch"):
        assert "fetch_data.py" in DOCKERFILE_DATA_STAGES[mode]


def _run_fetch(
    manifest: dict, dest: Path, tmp_path: Path, retries: int = 1
) -> subprocess.CompletedProcess:
    manifest_path = tmp_path / "data_manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    return subprocess.run(
        [
            sys.executable, str(FETCH),
            "--manifest", str(manifest_path),
            "--dest", str(dest),
            "--retries", str(retries),
        ],
        capture_output=True,
        text=True,
    )


def test_fetch_downloads_from_the_mirror_and_verifies(tmp_path):
    payload = b"evaluation data"
    mirror = tmp_path / "mirror" / "x.csv"
    mirror.parent.mkdir()
    mirror.write_bytes(payload)
    entry = {"path": "nested/x.csv", "size": len(payload), "sha256": _sha(payload), "url": mirror.as_uri()}
    result = _run_fetch({"files": [entry]}, tmp_path / "data", tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "data" / "nested" / "x.csv").read_bytes() == payload


def test_fetch_accepts_files_already_staged_in_the_build_context(tmp_path):
    payload = b"staged"
    dest = tmp_path / "data"
    dest.mkdir()
    (dest / "x.csv").write_bytes(payload)
    entry = {"path": "x.csv", "size": len(payload), "sha256": _sha(payload)}
    assert _run_fetch({"files": [entry]}, dest, tmp_path).returncode == 0


@pytest.mark.parametrize("staged", [True, False])
def test_fetch_fails_on_checksum_mismatch(tmp_path, staged):
    payload = b"tampered"
    dest = tmp_path / "data"
    dest.mkdir()
    entry = {"path": "x.csv", "size": len(payload), "sha256": "0" * 64}
    if staged:
        (dest / "x.csv").write_bytes(payload)
    else:
        mirror = tmp_path / "x.csv"
        mirror.write_bytes(payload)
        entry["url"] = mirror.as_uri()
    result = _run_fetch({"files": [entry]}, dest, tmp_path)
    assert result.returncode == 1
    assert "checksum mismatch" in result.stderr


def test_fetch_fails_when_a_file_is_neither_staged_nor_mirrored(tmp_path):
    entry = {"path": "x.csv", "size": 3, "sha256": "0" * 64}
    result = _run_fetch({"files": [entry]}, tmp_path / "data", tmp_path)
    assert result.returncode == 1
    assert "no mirror URL" in result.stderr


def test_fetch_retries_a_truncated_download(tmp_path):
    """A connection dropped mid-body must not be accepted, and must be retried."""
    import http.server
    import threading

    payload = b"x" * 200_000
    calls = {"n": 0}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            calls["n"] += 1
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            # First response is cut short; later ones are complete.
            self.wfile.write(payload[:1000] if calls["n"] == 1 else payload)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        entry = {
            "path": "x.bin",
            "size": len(payload),
            "sha256": _sha(payload),
            "url": f"http://127.0.0.1:{server.server_port}/x.bin",
        }
        fetch = _load_fetch_module()
        fetch.time.sleep = lambda seconds: None  # do not wait between attempts
        target = tmp_path / "data" / "x.bin"
        target.parent.mkdir()
        fetch.download(entry, target, retries=3)
    finally:
        server.shutdown()
    assert target.read_bytes() == payload
    assert calls["n"] == 2
    assert not target.with_name("x.bin.part").exists()


def _load_fetch_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("latchbio_fetch_data", FETCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fetch_rejects_a_staged_file_with_the_right_size_but_wrong_content(tmp_path):
    dest = tmp_path / "data"
    dest.mkdir()
    (dest / "x.csv").write_bytes(b"AAAA")
    entry = {"path": "x.csv", "size": 4, "sha256": _sha(b"BBBB")}
    result = _run_fetch({"files": [entry]}, dest, tmp_path)
    assert result.returncode == 1 and "checksum mismatch" in result.stderr
