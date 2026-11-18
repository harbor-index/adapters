"""An invented table scene that stands in for the simulator on the CPU.

``MockEnv`` offers the parts of the native evaluation environment that Direct
mode touches, in the native shapes: three RGB camera frames, the joint and
gripper state, the measured end-effector poses and an instruction. Nothing
here comes from RoboDojo, and no physics is simulated.

The mock robot is a gantry: the six "joint" values of an arm are simply the
position and the rotation vector of its gripper. ``MockKinematics`` therefore
needs no solver, but it applies the same per-step motion limits as the real
controller, so a decision takes as many control steps as it would there.

The task: put the left gripper over the red disc and the right gripper over the
blue disc, lower both, and close both grippers.
"""

from __future__ import annotations

from functools import partial
import math
import random
from types import SimpleNamespace
from typing import Any

import numpy as np

from ..direct.contract import ARMS, STEP_ROTATION_RAD, STEP_TRANSLATION_M

CAMERAS = ("cam_head", "cam_left_wrist", "cam_right_wrist")
# cam_head looks straight down at the table. Up in the image is +x and left is
# +y, so the left arm appears on the left. ``solver.py`` repeats these numbers.
HEAD_X = (0.05, 0.55)
HEAD_Y = (-0.40, 0.40)
HEAD_PIXELS_PER_METER = 400
# Each wrist camera looks straight down from its gripper.
WRIST_SPAN_M = 0.20
WRIST_PIXELS_PER_METER = 800
START = {"left": (0.30, 0.20, 0.20), "right": (0.30, -0.20, 0.20)}
TARGET_COLORS = {"left": (220, 30, 30), "right": (30, 60, 220)}
GRIPPER_COLORS = {"left": (20, 140, 60), "right": (240, 150, 20)}
TABLE_COLOR = (205, 190, 160)
GRID_COLOR = (170, 155, 130)
GRID_M = 0.10
DISC_RADIUS_M = 0.025
REACH_TOLERANCE_M = 0.03
REACH_HEIGHT_M = 0.12
STEP_LIMIT = 100
CONTROL_HZ = 25
INSTRUCTION = (
    "Mock task, not a RoboDojo task. Put the left gripper over the red disc and the right "
    "gripper over the blue disc, lower both grippers to a height of 0.12 m or less, then close "
    "both grippers. cam_head looks straight down at the table: up in the image is +x, left is "
    "+y, the image spans x from 0.05 to 0.55 m and y from -0.40 to 0.40 m, and the grid lines "
    "are 0.1 m apart. The left gripper is the green square and the right gripper is the orange "
    "square; a square shrinks as its gripper is lowered and gets a filled center when closed. "
    "Each wrist camera looks straight down from its own gripper and spans 0.2 m."
)


def _quaternion(rotation_vector) -> np.ndarray:
    angle = float(np.linalg.norm(rotation_vector))
    if angle < 1e-12:
        return np.array([1.0, 0.0, 0.0, 0.0])
    axis = np.asarray(rotation_vector, float) / angle
    return np.array([math.cos(angle / 2), *(axis * math.sin(angle / 2))])


def _rotation_vector(quaternion) -> np.ndarray:
    quaternion = np.asarray(quaternion, float)
    quaternion = quaternion / np.linalg.norm(quaternion)
    if quaternion[0] < 0:  # Take the short way around.
        quaternion = -quaternion
    sine = float(np.linalg.norm(quaternion[1:]))
    if sine < 1e-12:
        return np.zeros(3)
    return quaternion[1:] / sine * (2 * math.atan2(sine, quaternion[0]))


def _multiply(first, second) -> np.ndarray:
    w1, x1, y1, z1 = first
    w2, x2, y2, z2 = second
    return np.array([
        w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
        w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
        w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
        w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
    ])


def render(env: MockEnv, x_range: tuple[float, float], y_range: tuple[float, float],
           pixels_per_meter: int) -> np.ndarray:
    """Draw the table from above as an RGB frame of the given world rectangle."""
    height = round((x_range[1] - x_range[0]) * pixels_per_meter)
    width = round((y_range[1] - y_range[0]) * pixels_per_meter)
    pixel = 1.0 / pixels_per_meter
    xs = x_range[1] - (np.arange(height) + 0.5) * pixel
    ys = y_range[1] - (np.arange(width) + 0.5) * pixel
    x, y = np.meshgrid(xs, ys, indexing="ij")
    frame = np.empty((height, width, 3), np.uint8)
    frame[:] = TABLE_COLOR
    on_grid = (
        (np.abs(x / GRID_M - np.round(x / GRID_M)) * GRID_M < pixel)
        | (np.abs(y / GRID_M - np.round(y / GRID_M)) * GRID_M < pixel)
    )
    frame[on_grid] = GRID_COLOR
    for arm in ARMS:
        target_x, target_y = env.targets[arm]
        frame[(x - target_x) ** 2 + (y - target_y) ** 2 <= DISC_RADIUS_M ** 2] = TARGET_COLORS[arm]
    for arm in ARMS:
        position = env.joints[arm][:3]
        # A higher gripper is closer to the camera and so looks larger.
        half = 0.02 + 0.1 * float(position[2])
        distance = np.maximum(np.abs(x - position[0]), np.abs(y - position[1]))
        frame[(distance <= half) & (distance > half - 2.5 * pixel)] = GRIPPER_COLORS[arm]
        if env.opening[arm] <= 0.5:
            frame[distance <= 0.008] = GRIPPER_COLORS[arm]
    return frame


class MockEnv:
    """One episode of the mock task, with the native attributes Direct mode reads."""

    num_envs = 1

    def __init__(self, task_name: str, layout_id: int, eval_seed: int = 0, step_lim: int = STEP_LIMIT):
        self.task_name, self.step_lim, self.eval_seed = task_name, step_lim, eval_seed
        self.env_seeds = [layout_id]
        layout = random.Random(f"{eval_seed}:{layout_id}")
        self.targets = {}
        for arm in ARMS:
            angle, radius = layout.uniform(0, 2 * math.pi), layout.uniform(0.05, 0.09)
            self.targets[arm] = (
                START[arm][0] + radius * math.cos(angle), START[arm][1] + radius * math.sin(angle),
            )
        self.joints = {arm: np.array([*START[arm], 0.0, 0.0, 0.0]) for arm in ARMS}
        self.opening = {arm: 1.0 for arm in ARMS}
        self.take_action_cnt = [0]
        self.success = [False]
        self.end_flag = [False]
        self.obs_manager = SimpleNamespace(collect_freq=CONTROL_HZ)
        self.reward_manager = SimpleNamespace(
            check_list=[[partial(self.reached, arm) for arm in ARMS]],
            final_check_list=[[]], trigger_check_list=[[]],
        )

    def pose(self, arm: str) -> list[float]:
        joints = self.joints[arm]
        return [*map(float, joints[:3]), *map(float, _quaternion(joints[3:]))]

    def reached(self, arm: str) -> bool:
        x, y, z = self.joints[arm][:3]
        target_x, target_y = self.targets[arm]
        return bool(
            math.hypot(x - target_x, y - target_y) <= REACH_TOLERANCE_M
            and z <= REACH_HEIGHT_M and self.opening[arm] <= 0.5
        )

    def score(self) -> float:
        """Success scores 1; otherwise each arm that finished its part scores a half."""
        return sum(self.reached(arm) for arm in ARMS) / len(ARMS)

    def is_episode_end(self) -> bool:
        if not self.end_flag[0]:
            if all(self.reached(arm) for arm in ARMS):
                self.end_flag[0], self.success[0] = True, True
            elif self.take_action_cnt[0] >= self.step_lim:
                self.end_flag[0], self.success[0] = True, False
        return self.end_flag[0]

    def get_obs(self) -> dict[str, Any]:
        state = {}
        for arm in ARMS:
            state[f"{arm}_ee_pose"] = np.array(self.pose(arm), np.float32)
            state[f"{arm}_arm_joint_state"] = self.joints[arm].astype(np.float32)
            state[f"{arm}_ee_joint_state"] = np.array([self.opening[arm]], np.float32)
        vision = {"cam_head": {"color": render(self, HEAD_X, HEAD_Y, HEAD_PIXELS_PER_METER)}}
        for arm in ARMS:
            x, y = self.joints[arm][:2]
            half = WRIST_SPAN_M / 2
            vision[f"cam_{arm}_wrist"] = {"color": render(
                self, (x - half, x + half), (y - half, y + half), WRIST_PIXELS_PER_METER,
            )}
        return {"vision": vision, "state": state, "instruction": INSTRUCTION, "env_idx": 0}

    def take_action(self, action: dict[str, Any]) -> None:
        """Advance one control step. The mock arm reaches its joint command exactly."""
        for arm in ARMS:
            self.joints[arm] = np.asarray(action[f"{arm}_arm_joint_state"], float).copy()
            self.opening[arm] = float(action[f"{arm}_ee_joint_state"][0])
        self.take_action_cnt[0] += 1


class MockKinematics:
    """Bounded step toward the target pose, in place of the robot's inverse kinematics."""

    def __init__(self, env: MockEnv):
        self.env = env

    def check(self) -> dict[str, Any]:
        return {"passed": True, "method": "mock-gantry", "arms": {}}

    def target(self, targets: dict[str, Any]) -> dict[str, Any]:
        action, remaining = [], {}
        for arm in ARMS:
            joints = self.env.joints[arm]
            offset = np.asarray(targets[arm]["position"], float) - joints[:3]
            distance = float(np.linalg.norm(offset))
            if distance > STEP_TRANSLATION_M:
                offset *= STEP_TRANSLATION_M / distance
            current = _quaternion(joints[3:])
            wanted = np.asarray(targets[arm]["quaternion_wxyz"], float)
            # Rotation that is still missing, limited to one step.
            turn = _rotation_vector(_multiply(wanted, current * np.array([1, -1, -1, -1])))
            angle = float(np.linalg.norm(turn))
            if angle > STEP_ROTATION_RAD:
                turn *= STEP_ROTATION_RAD / angle
            rotation = _rotation_vector(_multiply(_quaternion(turn), current))
            action.extend([*(joints[:3] + offset), *rotation, 0.0 if targets[arm]["gripper_closed"] else 1.0])
            remaining[arm] = {"position_error_m": distance, "rotation_error_rad": angle}
        return {"action": action, "diagnostics": {"method": "mock-gantry", "before_step": remaining}}
