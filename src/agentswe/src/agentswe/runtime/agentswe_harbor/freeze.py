from __future__ import annotations

import socket

from .devserver import FREEZE_SOCKET


def main() -> None:
    """[[verifier.collect]] hook: upstream's DevController.freeze_after_builder_exit, once the Builder has stopped."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(FREEZE_SOCKET))
        response = client.makefile("r", encoding="utf-8").readline()
    if not response:
        raise SystemExit("the dev controller failed to freeze the delivery (see the evaluator's log)")
    print(response.strip())


if __name__ == "__main__":
    main()
