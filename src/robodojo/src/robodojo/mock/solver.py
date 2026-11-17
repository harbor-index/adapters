#!/usr/bin/env python3
"""Scripted player for the mock task. It needs no model and no API key.

It plays through the bridge client exactly as an agent would: it reads the
saved observation, finds the two discs in the ``cam_head`` picture, and sends
bounded decisions until the bridge reports that every episode is finished.
It is the oracle of the generated mock task, where it runs in the agent
container, and it only works for the mock scene.

Only the standard library is used, and nothing is imported from the adapter.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
import json
import math
from pathlib import Path
import struct
import subprocess
import sys
import time
import zlib

# Camera geometry and colors of ``scene.py``; a test keeps the two in step.
HEAD_X_MAX = 0.55
HEAD_Y_MAX = 0.40
HEAD_PIXELS_PER_METER = 400
TARGET_COLORS = {"left": (220, 30, 30), "right": (30, 60, 220)}
GOAL_HEIGHT_M = 0.10
MOVE_M = 0.045  # Below the 0.05 m limit of the action contract.
WAIT_SECONDS = "600"
STARTUP_SECONDS = 300.0


def decode_png(data: bytes) -> tuple[int, int, bytes]:
    """Decode the plain 8-bit RGB pictures that the bridge serves."""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("Not a PNG file")
    offset, header, compressed = 8, None, b""
    while offset < len(data):
        length, kind = struct.unpack("!I4s", data[offset:offset + 8])
        body = data[offset + 8:offset + 8 + length]
        if kind == b"IHDR":
            header = struct.unpack("!IIBBBBB", body)
        elif kind == b"IDAT":
            compressed += body
        offset += 12 + length
    if header is None or header[2:] != (8, 2, 0, 0, 0):
        raise ValueError("Only 8-bit RGB pictures without interlacing are supported")
    width, height = header[:2]
    raw, stride = zlib.decompress(compressed), width * 3
    rows = []
    for row in range(height):
        start = row * (stride + 1)
        if raw[start] != 0:
            raise ValueError("Unexpected PNG row filter")
        rows.append(raw[start + 1:start + 1 + stride])
    return width, height, b"".join(rows)


def locate(picture: bytes) -> dict[str, tuple[float, float]]:
    """Return the table position of each arm's disc from the top-down picture."""
    width, height, pixels = decode_png(picture)
    targets = {}
    for arm, color in TARGET_COLORS.items():
        wanted, count, rows, columns = bytes(color), 0, 0, 0
        for index in range(width * height):
            if pixels[index * 3:index * 3 + 3] == wanted:
                count, rows, columns = count + 1, rows + index // width, columns + index % width
        if not count:
            raise SystemExit(f"The {arm} arm's disc is not visible in the head camera picture")
        targets[arm] = (
            HEAD_X_MAX - (rows / count + 0.5) / HEAD_PIXELS_PER_METER,
            HEAD_Y_MAX - (columns / count + 0.5) / HEAD_PIXELS_PER_METER,
        )
    return targets


def plan(observation: dict, targets: dict[str, tuple[float, float]]) -> dict:
    """One bounded decision: move each gripper toward the point above its disc."""
    target = {}
    for arm, (x, y) in targets.items():
        current = observation["current_eef"][arm]
        position = current["position"]
        offset = [x - position[0], y - position[1], GOAL_HEIGHT_M - position[2]]
        distance = math.sqrt(sum(value * value for value in offset))
        scale = min(1.0, MOVE_M / distance) if distance > 0 else 0.0
        norm = math.sqrt(sum(value * value for value in current["quaternion_wxyz"]))
        target[arm] = {
            "position": [position[index] + offset[index] * scale for index in range(3)],
            "quaternion_wxyz": [value / norm for value in current["quaternion_wxyz"]],
            # Close on the move that arrives.
            "gripper_closed": distance <= MOVE_M,
        }
    return {
        "request_id": observation["request_id"], "mode": "eef", "steps": 5,
        "reason": "Scripted mock solver: move each gripper to its disc and close it.",
        "target": target,
    }


def solve(call: Callable[..., dict], startup_seconds: float = STARTUP_SECONDS) -> int:
    """Play until the bridge reports the end. ``call`` runs one bridge command."""
    # The agent container can start before the service has opened its bridge.
    deadline = time.monotonic() + startup_seconds
    while call("status").get("status") == "transport_error":
        if time.monotonic() >= deadline:
            raise SystemExit("Mock solver stopped: the bridge did not become reachable")
        time.sleep(0.5)
    targets: dict[str, tuple[float, float]] = {}
    decisions = 0
    result = call("observe", "--wait", WAIT_SECONDS)
    while True:
        status = result.get("status")
        if status == "finished":
            print(f"Mock solver finished after {decisions} decisions.")
            return 0
        if status in ("waiting", "episode_end"):
            result = call("observe", "--wait", WAIT_SECONDS)
            continue
        if status != "observation":
            raise SystemExit(f"Mock solver stopped: {json.dumps(result)[:500]}")
        observation = result["observation"]
        if observation["step_id"] == 0:
            targets = locate((Path(result["observation_dir"]) / "cam_head.png").read_bytes())
        result = call(
            "act", "--wait", WAIT_SECONDS, "--response", json.dumps(plan(observation, targets)),
        )
        if result.get("status") == "rejected":
            raise SystemExit(f"Mock solver decision was rejected: {result.get('error')}")
        decisions += 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bridge", default="/opt/robodojo/bridge_client.py", help="Path of the bridge client")
    parser.add_argument("--url", help="Bridge address; the bridge client's default if omitted")
    parser.add_argument("--out", help="Directory for saved observations; the client's default if omitted")
    args = parser.parse_args(argv)
    options = [item for name in ("url", "out") if getattr(args, name) for item in (f"--{name}", getattr(args, name))]

    def call(*command: str) -> dict:
        done = subprocess.run(
            [sys.executable, args.bridge, *options, *command], capture_output=True, text=True, check=False,
        )
        lines = [line for line in done.stdout.splitlines() if line.strip()]
        try:
            return json.loads(lines[-1])
        except (IndexError, ValueError):
            raise SystemExit(f"Bridge client failed ({done.returncode}): {done.stderr[-500:]}") from None

    return solve(call)


if __name__ == "__main__":
    raise SystemExit(main())
