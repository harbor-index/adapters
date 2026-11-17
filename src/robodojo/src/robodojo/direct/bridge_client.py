#!/usr/bin/env python3
"""Command-line client for the RoboDojo Direct bridge.

This single file is the only RoboDojo code in the agent container. It forwards
one call to the broker in the simulator container and prints one JSON line.

    bridge_client.py status
    bridge_client.py observe --wait 600
    bridge_client.py act --response '{"request_id": ...}' --wait 600
    bridge_client.py abort --reason "spending limit reached"

An observation is saved under ``--out/<request_id>/`` as ``observation.json``
plus one lossless PNG per camera.

Exit codes: 0 for a definite answer, including a rejected decision; 3 when the
transport failed. A failed ``act`` is never retried here: the caller cannot
know whether the decision was executed and must give up the run.

Only the standard library is used.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "http://simulator:19100"
DEFAULT_OUT = "/tmp/robodojo-direct"
# Extra seconds allowed beyond the broker-side wait before the transport gives up.
TRANSPORT_MARGIN = 30


class TransportError(RuntimeError):
    pass


def _request(url: str, payload: dict | None, timeout: float) -> tuple[int, bytes]:
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        url, data=data, method="GET" if data is None else "POST",
        headers={"Content-Type": "application/json"},
    )
    # The bridge is on the private Compose network; never route it via a proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except (OSError, urllib.error.URLError) as error:
        raise TransportError(f"{type(error).__name__}: {error}") from error


def _json(url: str, payload: dict | None, timeout: float) -> dict:
    code, body = _request(url, payload, timeout)
    try:
        value = json.loads(body)
    except ValueError as error:
        raise TransportError(f"Bridge returned HTTP {code} without JSON") from error
    if not isinstance(value, dict):
        raise TransportError("Bridge returned an unexpected JSON value")
    if code not in (200, 400, 409):
        raise TransportError(f"Bridge returned HTTP {code}: {value}")
    return value


def save_observation(base: str, result: dict, out: Path, timeout: float) -> dict:
    """Download the camera frames of an observation and store it on disk."""
    observation = result["observation"]
    request_id = observation["request_id"]
    if not request_id.isalnum():
        raise TransportError("Bridge returned an unsafe request_id")
    if out.exists():
        shutil.rmtree(out)  # Only the open observation is kept in this container.
    directory = out / request_id
    directory.mkdir(parents=True)
    for camera in observation["cameras"]:
        if not camera.replace("_", "").isalnum():
            raise TransportError("Bridge returned an unsafe camera name")
        query = urllib.parse.urlencode({"request_id": request_id, "camera": camera})
        code, body = _request(f"{base}/image?{query}", None, timeout)
        if code != 200 or not body.startswith(b"\x89PNG"):
            raise TransportError(f"Could not download the {camera} frame (HTTP {code})")
        (directory / f"{camera}.png").write_bytes(body)
    (directory / "observation.json").write_text(json.dumps(observation) + "\n", encoding="utf-8")
    return {**result, "observation_dir": str(directory)}


def main(argv: list[str] | None = None, write=print) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.environ.get("ROBODOJO_BRIDGE_URL", DEFAULT_URL))
    parser.add_argument("--out", type=Path, default=Path(os.environ.get("ROBODOJO_BRIDGE_OUT", DEFAULT_OUT)))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    observe = commands.add_parser("observe")
    observe.add_argument("--wait", type=float, default=0)
    act = commands.add_parser("act")
    act.add_argument("--response", required=True, help="Decision as a JSON object")
    act.add_argument("--wait", type=float, default=600)
    abort = commands.add_parser("abort")
    abort.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    base = args.url.rstrip("/")
    try:
        if args.command == "status":
            result = _json(f"{base}/status", None, TRANSPORT_MARGIN)
        elif args.command == "abort":
            result = _json(f"{base}/abort", {"reason": args.reason}, TRANSPORT_MARGIN)
        else:
            if args.command == "observe":
                payload, path = {"wait": args.wait}, "/observe"
            else:
                try:
                    response = json.loads(args.response)
                except ValueError as error:
                    parser.error(f"--response is not valid JSON: {error}")
                payload, path = {"response": response, "wait": args.wait}, "/act"
            result = _json(f"{base}{path}", payload, args.wait + TRANSPORT_MARGIN)
            if result.get("status") == "observation":
                result = save_observation(base, result, args.out, TRANSPORT_MARGIN)
    except TransportError as error:
        write(json.dumps({"status": "transport_error", "error": str(error)}))
        return 3
    write(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
