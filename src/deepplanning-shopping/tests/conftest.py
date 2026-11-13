import os
import socket
from pathlib import Path

import pytest

from deepplanning_shopping.adapter import DeepPlanningShoppingAdapter


@pytest.fixture(autouse=True)
def block_external_network(monkeypatch):
    """Tests can use loopback fixtures, never an inference or download endpoint."""
    connect = socket.socket.connect
    getaddrinfo = socket.getaddrinfo

    def allowed(host):
        return host in ("127.0.0.1", "::1", "localhost")

    def guarded_connect(sock, address):
        if isinstance(address, tuple) and not allowed(address[0]):
            raise AssertionError(f"External network forbidden in tests: {address[0]}")
        return connect(sock, address)

    def guarded_getaddrinfo(host, *args, **kwargs):
        if not allowed(host):
            raise AssertionError(f"External network forbidden in tests: {host}")
        return getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)


@pytest.fixture(scope="session")
def inputs():
    source = os.environ.get("DEEPPLANNING_SOURCE_DIR")
    data = os.environ.get("DEEPPLANNING_DATA_DIR")
    if not source or not data:
        pytest.skip(
            "Set DEEPPLANNING_SOURCE_DIR and DEEPPLANNING_DATA_DIR for pinned-source tests"
        )
    return {"source_dir": Path(source), "data_dir": Path(data)}


@pytest.fixture(scope="session")
def generated(tmp_path_factory, inputs):
    output = tmp_path_factory.mktemp("generated") / "tasks"
    paths = DeepPlanningShoppingAdapter(output, **inputs).run()
    assert len(paths) == 100
    return output
