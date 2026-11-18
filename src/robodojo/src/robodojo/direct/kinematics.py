"""Robot-only forward kinematics and bounded local inverse kinematics.

Adapted from ``hybrid_rollout/robodojo/robodojo_server/kinematics.py`` and
``action_edit_kinematics.py`` in GPT-as-Policy (MIT, revision 8f3d362b; see
``GPT_AS_POLICY_LICENSE``). The hybrid proposal preview was removed and the
limits are named constants; the numerical method is unchanged.

Nothing here reads object poses, rewards or other scene truth, and nothing
advances physics. The target is converted to a joint command by one damped
least-squares update that is recomputed after every executed control step.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import numpy as np
from scipy.spatial.transform import Rotation

from .contract import (
    ARMS,
    QUATERNION_NORM_TOLERANCE,
    STEP_JOINT_RAD,
    STEP_ROTATION_RAD,
    STEP_TRANSLATION_M,
)

DAMPING = 0.05
FK_POSITION_TOLERANCE_M = 0.002
FK_ROTATION_TOLERANCE_RAD = 0.01


def transform(position, quaternion_wxyz) -> np.ndarray:
    quaternion = np.asarray(quaternion_wxyz, float)
    matrix = np.eye(4)
    matrix[:3, :3] = Rotation.from_quat(quaternion[[1, 2, 3, 0]]).as_matrix()
    matrix[:3, 3] = position
    return matrix


def _bounded(vector: np.ndarray, cap: float) -> np.ndarray:
    return vector * min(1.0, cap / max(np.linalg.norm(vector), 1e-12))


class ArmFK:
    """Kinematic chain of one arm, read from its URDF."""

    def __init__(self, urdf, joint_names, base="base_link", tip="link6"):
        self.names = list(joint_names)
        edges = {
            joint.find("child").get("link"): joint
            for joint in ET.parse(urdf).getroot().findall("joint")
        }
        self.chain = []
        while tip != base:
            if tip not in edges:
                raise ValueError(f"URDF has no chain from {base} to the requested tip")
            joint = edges[tip]
            origin = joint.find("origin")
            xyz = np.array(origin.get("xyz", "0 0 0").split(), float) if origin is not None else np.zeros(3)
            rpy = np.array(origin.get("rpy", "0 0 0").split(), float) if origin is not None else np.zeros(3)
            offset = np.eye(4)
            offset[:3, :3] = Rotation.from_euler("xyz", rpy).as_matrix()
            offset[:3, 3] = xyz
            axis = joint.find("axis")
            axis = np.array(axis.get("xyz").split(), float) if axis is not None else np.array([1.0, 0, 0])
            self.chain.insert(0, (joint.get("name"), joint.get("type"), offset, axis))
            tip = joint.find("parent").get("link")
        moving = [name for name, kind, _, _ in self.chain if kind != "fixed"]
        if set(moving) != set(self.names):
            raise ValueError(f"URDF chain and arm joints differ: {moving}, {self.names}")

    def matrix(self, joints) -> np.ndarray:
        joints = np.asarray(joints, float)
        if joints.shape != (len(self.names),) or not np.isfinite(joints).all():
            raise ValueError("Invalid arm joint vector")
        values, result = dict(zip(self.names, joints, strict=True)), np.eye(4)
        for name, kind, offset, axis in self.chain:
            motion = np.eye(4)
            if kind in ("revolute", "continuous"):
                motion[:3, :3] = Rotation.from_rotvec(axis * values[name]).as_matrix()
            elif kind != "fixed":
                raise ValueError("Only fixed and revolute arm joints are supported")
            result = result @ offset @ motion
        return result

    def bounded_target(self, joints, limits, root, target):
        """One bounded damped least-squares update toward ``target``."""
        joints = np.asarray(joints, float)
        now = root @ self.matrix(joints)
        goal = transform(target["position"], target["quaternion_wxyz"])
        translation = goal[:3, 3] - now[:3, 3]
        rotation = Rotation.from_matrix(goal[:3, :3] @ now[:3, :3].T).as_rotvec()
        error = np.r_[_bounded(translation, STEP_TRANSLATION_M), _bounded(rotation, STEP_ROTATION_RAD)]
        jacobian = np.zeros((6, len(joints)))
        for index in range(len(joints)):
            shifted = joints.copy()
            shifted[index] += 1e-5
            moved = root @ self.matrix(shifted)
            jacobian[:3, index] = (moved[:3, 3] - now[:3, 3]) / 1e-5
            jacobian[3:, index] = Rotation.from_matrix(moved[:3, :3] @ now[:3, :3].T).as_rotvec() / 1e-5
        delta = jacobian.T @ np.linalg.solve(jacobian @ jacobian.T + DAMPING**2 * np.eye(6), error)
        limits = np.asarray(limits, float)
        low = np.maximum(limits[:, 0], joints - STEP_JOINT_RAD)
        high = np.minimum(limits[:, 1], joints + STEP_JOINT_RAD)
        if np.any(low > high):
            raise ValueError("A joint is outside its valid interval")
        result = np.clip(joints + delta, low, high).astype(np.float32)
        diagnostics = {
            "target_position_error_m": float(np.linalg.norm(translation)),
            "target_rotation_error_rad": float(np.linalg.norm(rotation)),
            "proposed_joint_delta": (result - joints).tolist(),
            "method": "robot_only_numerical_jacobian_dls",
        }
        return result, diagnostics


class DualKinematics:
    """Bind the two target arms of a native RoboDojo environment to ``ArmFK``."""

    def __init__(self, env):
        self.manager = env.robot_manager
        self.robots = {
            robot.arm_name.split("_")[0]: robot
            for robot in self.manager.robot_list
            if robot.type == "target"
        }
        if set(self.robots) != set(ARMS):
            raise ValueError("Expected exactly the left and right target arms")
        self.fk = {
            arm: ArmFK(robot.urdf_path, robot.arm_joints_name, robot.base_link, robot.ee_link_name)
            for arm, robot in self.robots.items()
        }

    def root(self, arm) -> np.ndarray:
        robot = self.robots[arm]
        value = np.asarray(self.manager.get_link_pose(robot, robot.base_link, is_relative=True)[0], float)
        return transform(value[:3], value[3:])

    def joints(self, arm) -> np.ndarray:
        return np.asarray(self.manager.get_joint(self.robots[arm])[0], float)

    def limits(self, arm) -> np.ndarray:
        robot = self.robots[arm]
        articulation = self.manager.robot_key[self.manager.robot_list.index(robot)]
        limits = articulation.data.soft_joint_pos_limits[0, robot.arm_joint_indices]
        return np.asarray(limits.cpu().numpy() if hasattr(limits, "cpu") else limits, float)

    def check(self) -> dict:
        """Fail closed unless URDF kinematics reproduce the measured link pose."""
        checks = {}
        for arm, robot in self.robots.items():
            predicted = self.root(arm) @ self.fk[arm].matrix(self.joints(arm))
            measured = np.asarray(self.manager.get_real_endpose(robot)[0], float)
            position_error = float(np.linalg.norm(predicted[:3, 3] - measured[:3]))
            rotation_error = float(Rotation.from_matrix(
                predicted[:3, :3] @ transform(measured[:3], measured[3:])[:3, :3].T
            ).magnitude())
            checks[arm] = {
                "position_error_m": position_error,
                "rotation_error_rad": rotation_error,
                "passed": bool(
                    position_error < FK_POSITION_TOLERANCE_M
                    and rotation_error < FK_ROTATION_TOLERANCE_RAD
                ),
            }
        if not all(check["passed"] for check in checks.values()):
            raise ValueError(f"Robot kinematics do not match the measured end-effector pose: {checks}")
        return {"arms": checks, "passed": True}

    def target(self, targets) -> dict:
        """Convert left and right targets to one native 14-value joint action."""
        if set(targets) != set(ARMS):
            raise ValueError("Explicit left and right targets are required")
        self.check()
        action, diagnostics = [], {}
        for arm in ARMS:
            target = targets[arm]
            position = np.asarray(target["position"], float)
            quaternion = np.asarray(target["quaternion_wxyz"], float)
            if (
                position.shape != (3,) or quaternion.shape != (4,)
                or not np.isfinite(np.r_[position, quaternion]).all()
                or abs(np.linalg.norm(quaternion) - 1) > QUATERNION_NORM_TOLERANCE
            ):
                raise ValueError("A finite position and a unit wxyz quaternion are required")
            if type(target["gripper_closed"]) is not bool:
                raise ValueError("An explicit boolean gripper command is required")
            joints, diagnostics[arm] = self.fk[arm].bounded_target(
                self.joints(arm), self.limits(arm), self.root(arm), target
            )
            # Native opening is 0 for closed and 1 for open.
            action.extend([*joints.tolist(), 0.0 if target["gripper_closed"] else 1.0])
        return {"action": action, "diagnostics": diagnostics}
