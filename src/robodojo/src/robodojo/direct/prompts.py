"""Prompt and message construction for the Direct scaffold.

The scaffold gives the model exactly one tool and no shell, file or image
tools. Everything the model knows about the task comes from the unchanged
native instruction, the camera frames and the robot state. No task-specific
hints are added. ``PROMPT_SHA256`` identifies this scaffold in provenance; any
edit to the system prompt or the tool definition changes it.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from .contract import (
    CONTRACT_VERSION,
    MAX_ROTATION_RAD,
    MAX_STEPS,
    MAX_TRANSLATION_M,
    STEP_JOINT_RAD,
    STEP_ROTATION_RAD,
    STEP_TRANSLATION_M,
    TOOL_NAME,
    decision_schema,
)

SYSTEM_PROMPT = f"""\
You control a bimanual robot in the RoboDojo simulation benchmark. You are the robot's policy: \
each turn you receive camera frames and the measured robot state, and you answer by calling the \
`{TOOL_NAME}` tool once with the next bounded motion for both arms. Keep acting until the episode \
ends. You cannot stop early, reset, undo an action, or ask questions.

Robot and observations
- Two ARX X5 arms, `left` and `right`, each with a parallel gripper.
- Every observation has one frame per camera (normally `cam_head`, `cam_left_wrist` and \
`cam_right_wrist`), the task instruction, the step counters, and the robot state.
- `current_eef.left` and `current_eef.right` are the measured poses of each arm's last link \
(`link6`): `position` in meters and `quaternion_wxyz` as a unit quaternion in w, x, y, z order. \
Both arms use the same environment-origin coordinate frame.
- `joint_state` lists six measured arm joint angles in radians per arm.
- `gripper_opening_command` is the last commanded opening, from 0 (closed) to 1 (open). It is not \
a measured finger gap and does not prove that an object is grasped.
- You are never given object poses, rewards or progress scores. Judge the scene from the frames.

Actions
- Call `{TOOL_NAME}` with `request_id` copied from the current observation, `mode` set to `eef`, \
`steps`, `reason`, and a `target` for both `left` and `right`.
- Each target has `position`, `quaternion_wxyz` and `gripper_closed`. `gripper_closed: true` \
commands a closed gripper, `false` an open one.
- Each target must stay within {MAX_TRANSLATION_M} m and {MAX_ROTATION_RAD} rad of that arm's \
current measured pose. Larger motions take several decisions.
- `steps` is 1 to {MAX_STEPS} control steps at 25 Hz. On every step a local inverse-kinematics \
update moves each arm toward its target by at most {STEP_TRANSLATION_M} m, \
{STEP_ROTATION_RAD} rad, and {STEP_JOINT_RAD} rad per joint. A target is therefore not proof of \
arrival or contact: check the next frames and measured poses.
- To hold an arm still, send its current measured pose and the gripper command you want.
- `reason` is one or two English sentences naming the visible evidence and the purpose of the \
motion. It is also your only memory of earlier frames, so record anything you must remember.
- A rejected call executes nothing. Read the error, correct the call, and send it again for the \
same `request_id`.

Episode
- `remaining_steps` counts the control steps left. The simulator ends the episode when its own \
success conditions hold or when the steps run out; reaching the limit without success is a \
failure. Apparent visual completion is not success until the episode ends.
- Follow the task instruction exactly as written.
"""


def tool_definition() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": (
                "Move both robot arms toward bounded end-effector targets for a few control "
                "steps, then receive the next observation."
            ),
            "parameters": decision_schema(),
        },
    }


PROMPT_SHA256 = hashlib.sha256(json.dumps(
    {"system": SYSTEM_PROMPT, "tool": tool_definition(), "contract": CONTRACT_VERSION},
    sort_keys=True,
).encode()).hexdigest()


def observation_text(observation: dict[str, Any]) -> str:
    """Text part of one observation turn. Camera frames are attached separately."""
    fields = {
        key: observation[key]
        for key in (
            "request_id", "instruction", "step_id", "max_episode_steps", "remaining_steps",
            "current_eef", "joint_state", "cameras", "previous_result",
        )
    }
    previous = fields["previous_result"]
    if previous is not None:
        # Per-step solver diagnostics are kept in the logs, not in the prompt.
        fields["previous_result"] = {
            key: previous[key]
            for key in ("requested_steps", "executed_steps", "start_step_id", "end_step_id")
        }
    return json.dumps(fields, indent=1)


def image_part(png: bytes) -> dict[str, Any]:
    return {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()},
    }


def build_messages(turns: list[dict[str, Any]], *, image_history: int, history_window: int) -> list[dict[str, Any]]:
    """Render the episode so far in the chat-completions tool-calling format.

    ``turns`` holds ``observation`` turns (text plus frames), ``attempt`` turns
    (one tool call and its result), ``reply`` turns (an answer that is not exactly
    one call of the tool)
    and scaffold ``note`` turns. Only the last ``image_history``
    observations keep their frames. With a positive ``history_window`` only
    that many most recent observations and their attempts are kept.
    """
    indices = [index for index, turn in enumerate(turns) if turn["kind"] == "observation"]
    if history_window > 0 and len(indices) > history_window:
        turns = turns[indices[-history_window]:]
        indices = [index for index, turn in enumerate(turns) if turn["kind"] == "observation"]
        dropped = True
    else:
        dropped = False
    with_images = set(indices[-image_history:]) if image_history > 0 else set()
    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if dropped:
        messages.append({
            "role": "user",
            "content": "Earlier turns of this episode were dropped to bound the context.",
        })
    for index, turn in enumerate(turns):
        if turn["kind"] == "observation":
            content: list[dict[str, Any]] = [{"type": "text", "text": turn["text"]}]
            if index in with_images:
                for camera, png in turn["images"].items():
                    content.append({"type": "text", "text": f"Camera {camera}:"})
                    content.append(image_part(png))
            else:
                content.append({"type": "text", "text": "(Frames of this earlier observation are omitted.)"})
            messages.append({"role": "user", "content": content})
        elif turn["kind"] == "note":
            messages.append({"role": "user", "content": turn["text"]})
        elif turn["kind"] == "reply":
            # Not exactly one call of the tool: replayed as text, with the reason.
            messages.append({"role": "assistant", "content": turn["text"] or "(no content)"})
            messages.append({"role": "user", "content": turn["result"]})
        else:
            messages.append({
                "role": "assistant", "content": turn.get("text") or None,
                "tool_calls": [{
                    "id": turn["call_id"], "type": "function",
                    "function": {"name": TOOL_NAME, "arguments": turn["arguments"]},
                }],
            })
            messages.append({"role": "tool", "tool_call_id": turn["call_id"], "content": turn["result"]})
    return messages
