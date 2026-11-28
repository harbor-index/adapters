from __future__ import annotations

import json
import socket
import sys

from .devserver import CONTROL_SOCKET


def main() -> int:
    """[[verifier.collect]] hook: upstream DevController.freeze_after_builder_exit after the Builder stops."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(CONTROL_SOCKET))
        client.sendall(b'{"action": "freeze"}\n')
        response = b""
        while not response.endswith(b"\n"):
            chunk = client.recv(65536)
            if not chunk:
                break
            response += chunk
    payload = json.loads(response.decode("utf-8"))
    print(json.dumps(payload, indent=2))
    return 0 if "frozen" in payload else 1


if __name__ == "__main__":
    sys.exit(main())
