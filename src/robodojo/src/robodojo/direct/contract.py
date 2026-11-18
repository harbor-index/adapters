"""The Direct action contract shared by the agent and the simulator service.

A decision moves both ARX X5 arms toward explicit end-effector targets for a
few native control steps. The limits follow the public Direct ("GPT-only")
controller of GPT-as-Policy (MIT, revision 8f3d362b), whose validation this
module re-implements without NumPy so that the Harbor host can run it too.

The simulator service is the authority: it validates every decision against
the pose it measured, whatever the agent checked beforehand.
"""

from __future__ import annotations

import math
from typing import Any

CONTRACT_VERSION = "direct-eef-v1"
ARMS = ("left", "right")
MAX_STEPS = 5
MAX_TRANSLATION_M = 0.05
MAX_ROTATION_RAD = 0.35
# Per control step limits applied by the simulator-side inverse kinematics.
STEP_TRANSLATION_M = 0.02
STEP_ROTATION_RAD = 0.1
STEP_JOINT_RAD = 0.05
QUATERNION_NORM_TOLERANCE = 1e-4
TOOL_NAME = "robodojo_act"


class DecisionError(ValueError):
    """A decision violates the contract. Nothing was executed."""


def decision_schema() -> dict[str, Any]:
    """JSON schema of one decision, used as the model's only tool."""
    number = {"type": "number"}
    arm = {
        "type": "object",
        "additionalProperties": False,
        "required": ["position", "quaternion_wxyz", "gripper_closed"],
        "properties": {
            "position": {"type": "array", "items": number, "minItems": 3, "maxItems": 3},
            "quaternion_wxyz": {"type": "array", "items": number, "minItems": 4, "maxItems": 4},
            "gripper_closed": {"type": "boolean"},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["request_id", "mode", "steps", "reason", "target"],
        "properties": {
            "request_id": {"type": "string"},
            "mode": {"type": "string", "enum": ["eef"]},
            "steps": {"type": "integer", "minimum": 1, "maximum": MAX_STEPS},
            "reason": {"type": "string"},
            "target": {
                "type": "object",
                "additionalProperties": False,
                "required": list(ARMS),
                "properties": {name: arm for name in ARMS},
            },
        },
    }


def _vector(value: Any, length: int, field: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise DecisionError(f"{field} must be a list of {length} numbers")
    result = []
    for item in value:
        if type(item) not in (int, float) or not math.isfinite(item):
            raise DecisionError(f"{field} must contain only finite numbers")
        result.append(float(item))
    return result


def rotation_angle(first: list[float], second: list[float]) -> float:
    """Angle in radians between two orientations given as wxyz quaternions."""
    norms = math.sqrt(sum(x * x for x in first)) * math.sqrt(sum(x * x for x in second))
    if norms == 0:
        raise DecisionError("Quaternion has zero length")
    dot = abs(sum(a * b for a, b in zip(first, second, strict=True))) / norms
    return 2 * math.acos(min(1.0, dot))


def validate_decision(response: Any, request: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized copy of ``response`` or raise ``DecisionError``.

    ``request`` is the observation the decision answers. It must hold the
    ``request_id`` and the measured ``current_eef`` pose of both arms.
    """
    if not isinstance(response, dict):
        raise DecisionError("Decision must be a JSON object")
    expected = {"request_id", "mode", "steps", "reason", "target"}
    if set(response) != expected:
        raise DecisionError(f"Decision must contain exactly: {', '.join(sorted(expected))}")
    if response["request_id"] != request["request_id"]:
        raise DecisionError("Decision is stale: request_id does not match the current observation")
    if response["mode"] != "eef":
        raise DecisionError("Only mode 'eef' is available")
    steps = response["steps"]
    if type(steps) is not int or not 1 <= steps <= MAX_STEPS:
        raise DecisionError(f"steps must be an integer from 1 through {MAX_STEPS}")
    reason = response["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise DecisionError("reason must briefly state the visible evidence and the purpose")
    target = response["target"]
    if not isinstance(target, dict) or set(target) != set(ARMS):
        raise DecisionError("target must contain explicit left and right arms")
    normalized = {}
    for arm in ARMS:
        value = target[arm]
        if not isinstance(value, dict) or set(value) != {"position", "quaternion_wxyz", "gripper_closed"}:
            raise DecisionError(
                f"target.{arm} must contain exactly position, quaternion_wxyz and gripper_closed"
            )
        position = _vector(value["position"], 3, f"target.{arm}.position")
        quaternion = _vector(value["quaternion_wxyz"], 4, f"target.{arm}.quaternion_wxyz")
        norm = math.sqrt(sum(x * x for x in quaternion))
        if abs(norm - 1) > QUATERNION_NORM_TOLERANCE:
            raise DecisionError(f"target.{arm}.quaternion_wxyz must be a unit quaternion")
        if type(value["gripper_closed"]) is not bool:
            raise DecisionError(f"target.{arm}.gripper_closed must be true or false")
        current = request["current_eef"][arm]
        distance = math.dist(position, current["position"])
        if distance > MAX_TRANSLATION_M + 1e-9:
            raise DecisionError(
                f"target.{arm} is {distance:.4f} m from the measured pose; "
                f"the limit is {MAX_TRANSLATION_M} m"
            )
        angle = rotation_angle(quaternion, current["quaternion_wxyz"])
        if angle > MAX_ROTATION_RAD + 1e-9:
            raise DecisionError(
                f"target.{arm} is {angle:.4f} rad from the measured orientation; "
                f"the limit is {MAX_ROTATION_RAD} rad"
            )
        normalized[arm] = {
            "position": position,
            "quaternion_wxyz": quaternion,
            "gripper_closed": value["gripper_closed"],
        }
    return {
        "request_id": response["request_id"],
        "mode": "eef",
        "steps": steps,
        "reason": reason,
        "target": normalized,
    }


def hold_decision(request: dict[str, Any], reason: str) -> dict[str, Any]:
    """A one-step decision that keeps both arms at their measured poses.

    The gripper repeats its last commanded opening. The scaffold uses this only
    after the model repeatedly failed to produce a valid decision.
    """
    target = {}
    for arm in ARMS:
        current = request["current_eef"][arm]
        target[arm] = {
            "position": [float(x) for x in current["position"]],
            "quaternion_wxyz": [float(x) for x in current["quaternion_wxyz"]],
            "gripper_closed": bool(current["gripper_opening_command"] < 0.5),
        }
    return {
        "request_id": request["request_id"],
        "mode": "eef",
        "steps": 1,
        "reason": reason,
        "target": target,
    }
