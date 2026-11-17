"""Relay between the native simulator client and the Harbor agent.

The broker lives in the simulator container, inside the native XPolicyLab
policy server. The simulator side posts observations and polls for decisions
through the native policy protocol. The agent side reaches the broker over
HTTP on the private Compose network.

The broker is the trusted record of what was executed. It validates every
decision against the measured robot pose, hands each accepted decision to the
simulator exactly once, and appends an episode ledger that the verifier later
cross-checks against the native result file. Scores never pass through here:
they come only from the native ``_result.json``.

Only the standard library is used.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import struct
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse
import uuid
import zlib

from .contract import ARMS, CONTRACT_VERSION, DecisionError, validate_decision

MAX_BODY_BYTES = 1024 * 1024
MAX_WAIT_SECONDS = 3600.0


class BrokerConflict(RuntimeError):
    """The call does not fit the current episode state. Nothing was executed."""


def encode_png(width: int, height: int, rgb: bytes) -> bytes:
    """Encode tightly packed 8-bit RGB rows as a PNG without third-party code."""
    stride = width * 3
    if width <= 0 or height <= 0 or len(rgb) != stride * height:
        raise ValueError("RGB buffer does not match the image size")
    rows = b"".join(b"\x00" + rgb[y * stride:(y + 1) * stride] for y in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))

    header = struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b"")
    )


def _plain(value: Any) -> Any:
    """Convert arrays and scalars from the simulator to JSON-compatible values."""
    if hasattr(value, "tolist"):
        return value.tolist()
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def build_observation(native: dict[str, Any], *, step_id: int, max_episode_steps: int) -> dict[str, Any]:
    """Select the agent-visible fields of one native observation.

    The instruction, the joint and gripper state and the measured end-effector
    poses are passed through unchanged. Camera frames are handled separately.
    """
    state = native["state"]
    current_eef = {}
    joint_state = {}
    for arm in ARMS:
        pose = [float(x) for x in _plain(state[f"{arm}_ee_pose"])]
        joints = [float(x) for x in _plain(state[f"{arm}_arm_joint_state"])]
        opening = [float(x) for x in _plain(state[f"{arm}_ee_joint_state"])]
        if len(pose) != 7 or len(joints) != 6 or len(opening) != 1:
            raise ValueError(f"Unexpected native state shape for the {arm} arm")
        joint_state[arm] = {"arm_joints_rad": joints, "gripper_opening_command": opening[0]}
        current_eef[arm] = {
            "position": pose[:3],
            "quaternion_wxyz": pose[3:],
            "gripper_opening_command": opening[0],
        }
    instruction = native["instruction"]
    if not isinstance(instruction, str):
        raise ValueError("Native instruction must be text")
    return {
        "instruction": instruction,
        "step_id": step_id,
        "max_episode_steps": max_episode_steps,
        "remaining_steps": max(0, max_episode_steps - step_id),
        "joint_state": joint_state,
        "current_eef": current_eef,
        "frame": "environment_origin",
        "eef_link": "link6",
    }


def _append_line(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


class Broker:
    """Thread-safe episode state machine. One episode and one request at a time."""

    def __init__(self, results_dir: Path, *, poll_interval: float = 0.5) -> None:
        self.results_dir = Path(results_dir)
        self.ledger_dir = self.results_dir / "broker"
        self.ledger_dir.mkdir(parents=True, exist_ok=True)
        if (self.ledger_dir / "episodes.jsonl").exists():
            # A restarted policy server could not tell which decisions ran.
            raise RuntimeError("Broker ledger already exists; refusing to continue an earlier run")
        self._condition = threading.Condition()
        self._poll_interval = poll_interval
        self._state = "idle"
        self._episode: dict[str, Any] | None = None
        self._request: dict[str, Any] | None = None
        self._images: dict[str, bytes] = {}
        self._decision: dict[str, Any] | None = None
        self._last_episode: dict[str, Any] | None = None
        self._episodes_started = 0
        self._episodes_ended = 0
        self._abort_reason: str | None = None

    # ----- simulator side -------------------------------------------------

    def begin_episode(self, meta: dict[str, Any]) -> dict[str, Any]:
        with self._condition:
            self._require_running()
            if self._state != "idle":
                raise BrokerConflict(f"Cannot begin an episode while {self._state}")
            index = self._episodes_started
            self._episodes_started += 1
            self._episode = {
                "episode_index": index,
                "episode_id": uuid.uuid4().hex,
                "variant": str(meta["variant"]),
                "layout_id": int(meta["layout_id"]),
                "layout_seed_group": int(meta["layout_seed_group"]),
                "max_episode_steps": int(meta["max_episode_steps"]),
                "control_dt": float(meta["control_dt"]),
                "contract_version": CONTRACT_VERSION,
                "decisions": 0,
                "rejected_responses": 0,
                "executed_steps": 0,
                "started_unix": time.time(),
                "kinematics_check": _plain(meta.get("kinematics_check")),
                "native_conditions": _plain(meta.get("native_conditions")),
            }
            self._state = "episode_open"
            self._condition.notify_all()
            return {"episode_id": self._episode["episode_id"], "episode_index": index}

    def post_observation(
        self, episode_id: str, observation: dict[str, Any], images: dict[str, bytes],
        previous_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._condition:
            self._require_running()
            episode = self._require_episode(episode_id)
            if self._state not in ("episode_open", "executing"):
                raise BrokerConflict(f"Cannot post an observation while {self._state}")
            self._finish_decision(previous_result)
            request_id = uuid.uuid4().hex
            self._request = {
                "request_id": request_id,
                "episode": {
                    key: episode[key]
                    for key in ("episode_index", "variant", "layout_id", "max_episode_steps", "control_dt")
                },
                "decision_index": episode["decisions"],
                "cameras": list(images),
                "image_sha256": {name: hashlib.sha256(data).hexdigest() for name, data in images.items()},
                "previous_result": _plain(previous_result),
                **observation,
            }
            self._images = dict(images)
            self._decision = None
            self._state = "awaiting_decision"
            self._condition.notify_all()
            return {"request_id": request_id}

    def poll_decision(self, request_id: str) -> dict[str, Any]:
        with self._condition:
            if self._abort_reason is not None:
                return {"status": "abort", "reason": self._abort_reason}
            if self._request is None or self._request["request_id"] != request_id:
                raise BrokerConflict("Poll does not match the open request")
            if self._state == "awaiting_decision":
                return {"status": "pending"}
            if self._state != "decided":
                raise BrokerConflict("The decision for this request was already delivered")
            self._state = "executing"
            self._condition.notify_all()
            return {"status": "decision", "response": self._decision}

    def end_episode(self, episode_id: str, summary: dict[str, Any]) -> dict[str, Any]:
        with self._condition:
            episode = self._require_episode(episode_id)
            if self._state not in ("episode_open", "executing"):
                raise BrokerConflict(f"Cannot end an episode while {self._state}")
            self._finish_decision(summary.get("previous_result"))
            record = {
                **episode,
                "ended_unix": time.time(),
                "control_steps": int(summary["control_steps"]),
                "ended_by": "native_termination",
                "native_success_observed": bool(summary["native_success_observed"]),
            }
            return self._close_episode(record)

    def abandon_episode(self, episode_id: str, error: str) -> dict[str, Any]:
        """Close an episode that native code interrupted. It can never be scored."""
        with self._condition:
            episode = self._require_episode(episode_id)
            record = {
                **episode, "ended_unix": time.time(), "ended_by": "native_exception",
                "error": str(error)[:2000],
            }
            return self._close_episode(record)

    # ----- agent side -----------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._condition:
            return self._status()

    def observe(self, wait: float) -> dict[str, Any]:
        """Return the open observation, or report why there is none yet."""
        deadline = time.monotonic() + self._bounded_wait(wait)
        with self._condition:
            while True:
                run = self._run_state()
                if self._abort_reason is not None:
                    return {"status": "aborted", "reason": self._abort_reason}
                if self._state == "awaiting_decision":
                    return {"status": "observation", "observation": self._request}
                if run["status"] == "failed":
                    return {"status": "failed", "error": run.get("error")}
                if run["status"] == "completed" and self._state == "idle":
                    return {"status": "finished", **self._status()}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {"status": "waiting", **self._status()}
                self._condition.wait(min(remaining, self._poll_interval))

    def act(self, response: Any, wait: float) -> dict[str, Any]:
        """Validate and enqueue one decision, then wait for its outcome."""
        deadline = time.monotonic() + self._bounded_wait(wait)
        with self._condition:
            self._require_running()
            if self._state != "awaiting_decision" or self._request is None or self._episode is None:
                raise BrokerConflict(f"No observation is awaiting a decision (state: {self._state})")
            request_id = self._request["request_id"]
            episode_index = self._episode["episode_index"]
            try:
                decision = validate_decision(response, self._request)
            except DecisionError as error:
                self._episode["rejected_responses"] += 1
                _append_line(self._decisions_path(), {
                    "event": "rejected", "request_id": request_id, "error": str(error),
                    "response": response if isinstance(response, dict) else repr(response)[:2000],
                })
                return {"status": "rejected", "error": str(error), "request_id": request_id}
            self._decision = decision
            self._episode["decisions"] += 1
            _append_line(self._decisions_path(), {
                "event": "accepted", "request_id": request_id,
                "decision_index": self._request["decision_index"],
                "step_id": self._request["step_id"],
                "image_sha256": self._request["image_sha256"],
                "current_eef": self._request["current_eef"], "response": decision,
            })
            self._state = "decided"
            self._condition.notify_all()
            while True:
                run = self._run_state()
                # Report the end of this episode before any later observation.
                if self._last_episode is not None and self._last_episode["episode_index"] == episode_index:
                    return {"status": "episode_end", "episode": self._last_episode}
                if self._state == "awaiting_decision" and self._request["request_id"] != request_id:
                    return {"status": "observation", "observation": self._request}
                if self._abort_reason is not None:
                    return {"status": "aborted", "reason": self._abort_reason}
                if run["status"] == "failed":
                    return {"status": "failed", "error": run.get("error")}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {"status": "executing", "request_id": request_id}
                self._condition.wait(min(remaining, self._poll_interval))

    def abort(self, reason: str) -> dict[str, Any]:
        """Stop the run. The evaluation becomes incomplete and yields no reward."""
        with self._condition:
            if self._abort_reason is None:
                self._abort_reason = str(reason)[:2000] or "unspecified"
                temporary = self.ledger_dir / "abort.json.tmp"
                temporary.write_text(json.dumps({
                    "reason": self._abort_reason, "unix": time.time(), **self._status(),
                }, allow_nan=False) + "\n", encoding="utf-8")
                temporary.replace(self.ledger_dir / "abort.json")
            self._condition.notify_all()
            return {"status": "aborted", "reason": self._abort_reason}

    def image(self, request_id: str, camera: str) -> bytes:
        with self._condition:
            if self._request is None or self._request["request_id"] != request_id:
                raise BrokerConflict("Images are available only for the open request")
            if camera not in self._images:
                raise BrokerConflict(f"Unknown camera: {camera}")
            return self._images[camera]

    # ----- internals ------------------------------------------------------

    @staticmethod
    def _bounded_wait(wait: Any) -> float:
        if type(wait) not in (int, float) or not 0 <= wait <= MAX_WAIT_SECONDS:
            raise ValueError(f"wait must be a number of seconds from 0 through {MAX_WAIT_SECONDS:g}")
        return float(wait)

    def _require_running(self) -> None:
        if self._abort_reason is not None:
            raise BrokerConflict(f"The run was aborted: {self._abort_reason}")

    def _require_episode(self, episode_id: str) -> dict[str, Any]:
        if self._episode is None or self._episode["episode_id"] != episode_id:
            raise BrokerConflict("Call does not match the open episode")
        return self._episode

    def _close_episode(self, record: dict[str, Any]) -> dict[str, Any]:
        """Write the final ledger record of the open episode and return to idle.

        The caller holds the lock. The record is on disk before any waiter can
        see the episode as ended.
        """
        _append_line(self.ledger_dir / "episodes.jsonl", record)
        self._last_episode = record
        self._episodes_ended += 1
        self._episode = self._request = self._decision = None
        self._images = {}
        self._state = "idle"
        self._condition.notify_all()
        return {"episode_index": record["episode_index"]}

    def _decisions_path(self) -> Path:
        assert self._episode is not None
        return self.ledger_dir / "decisions" / f"{self._episode['episode_index']:04d}.jsonl"

    def _finish_decision(self, result: dict[str, Any] | None) -> None:
        """Record what the simulator executed for the delivered decision."""
        assert self._episode is not None
        if self._state != "executing":
            if result is not None:
                raise BrokerConflict("Execution result reported without a delivered decision")
            return
        if result is None or self._request is None or result.get("request_id") != self._request["request_id"]:
            raise BrokerConflict("Execution result does not match the delivered decision")
        self._episode["executed_steps"] += int(result["executed_steps"])
        _append_line(self._decisions_path(), {"event": "executed", **_plain(result)})

    def _run_state(self) -> dict[str, Any]:
        """Read the supervisor's verdict on the whole native run, if any."""
        try:
            value = json.loads((self.results_dir / "supervisor.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"status": "running"}
        return value if isinstance(value, dict) and "status" in value else {"status": "running"}

    def _status(self) -> dict[str, Any]:
        return {
            "state": self._state,
            "run": self._run_state()["status"],
            "episodes_started": self._episodes_started,
            "episodes_ended": self._episodes_ended,
            "aborted": self._abort_reason is not None,
            "contract_version": CONTRACT_VERSION,
        }


def make_server(broker: Broker, host: str, port: int) -> ThreadingHTTPServer:
    """Build the agent-facing HTTP server. The caller runs ``serve_forever``."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send(self, code: int, body: bytes, content_type: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, value: dict[str, Any]) -> None:
            self._send(code, json.dumps(value, allow_nan=False).encode())

        def _guarded(self, call) -> None:
            try:
                call()
            except BrokerConflict as error:
                self._json(409, {"status": "conflict", "error": str(error)})
            except (ValueError, KeyError, TypeError) as error:
                self._json(400, {"status": "bad_request", "error": f"{type(error).__name__}: {error}"})

        def do_GET(self) -> None:
            url = urlparse(self.path)
            if url.path == "/status":
                self._guarded(lambda: self._json(200, broker.status()))
            elif url.path == "/image":
                query = parse_qs(url.query)
                self._guarded(lambda: self._send(
                    200, broker.image(query["request_id"][0], query["camera"][0]), "image/png",
                ))
            else:
                self._json(404, {"status": "not_found"})

        def do_POST(self) -> None:
            def handle() -> None:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= MAX_BODY_BYTES:
                    raise ValueError("Request body is too large")
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("Request body must be a JSON object")
                path = urlparse(self.path).path
                if path == "/observe":
                    self._json(200, broker.observe(body.get("wait", 0)))
                elif path == "/act":
                    self._json(200, broker.act(body["response"], body.get("wait", 0)))
                elif path == "/abort":
                    self._json(200, broker.abort(body.get("reason", "")))
                else:
                    self._json(404, {"status": "not_found"})

            self._guarded(handle)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server
