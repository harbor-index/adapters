"""Checks of URDF forward kinematics and the bounded local IK update."""

from pathlib import Path
import tempfile
import unittest

import numpy as np
from scipy.spatial.transform import Rotation

from robodojo.direct.contract import STEP_JOINT_RAD, STEP_TRANSLATION_M
from robodojo.direct.kinematics import ArmFK, DualKinematics, transform

JOINTS = [f"joint{index}" for index in range(1, 7)]
AXES = ["0 0 1", "0 1 0", "0 1 0", "1 0 0", "0 1 0", "1 0 0"]
OFFSETS = ["0 0 0.1", "0 0 0.05", "0.25 0 0", "0.2 0 0", "0.05 0 0", "0.05 0 0"]
LIMITS = np.tile([-3.0, 3.0], (6, 1))


def write_urdf(path):
    links = "".join(f'<link name="{name}"/>' for name in ["base_link", *[f"link{i}" for i in range(1, 7)], "tool"])
    joints = "".join(
        f'<joint name="{JOINTS[i]}" type="revolute"><parent link="{"base_link" if i == 0 else f"link{i}"}"/>'
        f'<child link="link{i + 1}"/><origin xyz="{OFFSETS[i]}" rpy="0 0 0"/><axis xyz="{AXES[i]}"/></joint>'
        for i in range(6)
    )
    fixed = '<joint name="tool_mount" type="fixed"><parent link="link6"/><child link="tool"/></joint>'
    path.write_text(f'<robot name="arm">{links}{joints}{fixed}</robot>')


class Articulation:
    def __init__(self):
        self.data = type("Data", (), {"soft_joint_pos_limits": LIMITS[None]})()


class Manager:
    """Stand-in for the native robot manager with two identical arms."""

    def __init__(self, urdf, offset=0.0):
        self.robot_list, self.robot_key, self.state = [], [], {}
        self.offset = offset
        for arm, y in (("left", 0.3), ("right", -0.3)):
            robot = type("Robot", (), {})()
            robot.arm_name, robot.type = f"{arm}_arm", "target"
            robot.urdf_path, robot.arm_joints_name = str(urdf), list(JOINTS)
            robot.base_link, robot.ee_link_name = "base_link", "link6"
            robot.arm_joint_indices = list(range(6))
            self.robot_list.append(robot)
            self.robot_key.append(Articulation())
            self.state[arm] = {"joints": np.array([0.1, -0.4, 0.8, 0.2, 0.5, -0.3]),
                               "root": transform([0.0, y, 0.7], [1.0, 0.0, 0.0, 0.0])}
        self.fk = ArmFK(urdf, JOINTS)

    def _arm(self, robot):
        return robot.arm_name.split("_")[0]

    def get_joint(self, robot):
        return {0: self.state[self._arm(robot)]["joints"].copy()}

    def _pose(self, matrix):
        quaternion = Rotation.from_matrix(matrix[:3, :3]).as_quat()[[3, 0, 1, 2]]
        return np.r_[matrix[:3, 3], quaternion]

    def get_link_pose(self, robot, link_name, is_relative=False):
        return {0: self._pose(self.state[self._arm(robot)]["root"])}

    def get_real_endpose(self, robot):
        state = self.state[self._arm(robot)]
        pose = self._pose(state["root"] @ self.fk.matrix(state["joints"]))
        pose[0] += self.offset
        return {0: pose}


class KinematicsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.urdf = Path(self.temporary.name) / "arm.urdf"
        write_urdf(self.urdf)

    def test_forward_kinematics_of_the_zero_pose(self):
        fk = ArmFK(self.urdf, JOINTS)
        np.testing.assert_allclose(fk.matrix(np.zeros(6))[:3, 3], [0.55, 0.0, 0.15], atol=1e-12)
        np.testing.assert_allclose(fk.matrix(np.zeros(6))[:3, :3], np.eye(3), atol=1e-12)
        with self.assertRaises(ValueError):
            fk.matrix(np.zeros(5))
        with self.assertRaises(ValueError):
            fk.matrix(np.r_[np.zeros(5), np.nan])

    def test_chain_must_match_the_arm_joints(self):
        with self.assertRaisesRegex(ValueError, "differ"):
            ArmFK(self.urdf, JOINTS[:5])
        with self.assertRaisesRegex(ValueError, "no chain"):
            ArmFK(self.urdf, JOINTS, tip="missing_link")

    def test_repeated_bounded_updates_converge_without_exceeding_step_limits(self):
        fk = ArmFK(self.urdf, JOINTS)
        joints = np.array([0.1, -0.4, 0.8, 0.2, 0.5, -0.3])
        root = transform([0.0, 0.3, 0.7], [1.0, 0.0, 0.0, 0.0])
        start = root @ fk.matrix(joints)
        goal = start.copy()
        goal[:3, 3] += [0.03, -0.02, 0.02]
        quaternion = Rotation.from_matrix(goal[:3, :3]).as_quat()[[3, 0, 1, 2]]
        target = {"position": goal[:3, 3].tolist(), "quaternion_wxyz": quaternion.tolist()}
        first_error = None
        for _ in range(40):
            before = (root @ fk.matrix(joints))[:3, 3]
            updated, diagnostics = fk.bounded_target(joints, LIMITS, root, target)
            first_error = first_error or diagnostics["target_position_error_m"]
            self.assertLessEqual(np.abs(updated - joints).max(), STEP_JOINT_RAD + 1e-6)
            moved = np.linalg.norm((root @ fk.matrix(updated))[:3, 3] - before)
            self.assertLessEqual(moved, STEP_TRANSLATION_M * 1.5)
            joints = updated.astype(float)
        final = np.linalg.norm((root @ fk.matrix(joints))[:3, 3] - goal[:3, 3])
        self.assertLess(final, 0.002)
        self.assertLess(final, first_error)

    def test_joint_limits_clip_the_update(self):
        fk = ArmFK(self.urdf, JOINTS)
        joints = np.zeros(6)
        limits = np.tile([-0.01, 0.01], (6, 1))
        target = {"position": [0.3, 0.3, 0.5], "quaternion_wxyz": [1.0, 0.0, 0.0, 0.0]}
        updated, _ = fk.bounded_target(joints, limits, np.eye(4), target)
        self.assertLessEqual(np.abs(updated).max(), 0.01 + 1e-6)
        with self.assertRaisesRegex(ValueError, "valid interval"):
            fk.bounded_target(np.full(6, 1.0), limits, np.eye(4), target)

    def test_dual_arm_action_layout_and_gripper_convention(self):
        env = type("Env", (), {"robot_manager": Manager(self.urdf)})()
        kinematics = DualKinematics(env)
        self.assertTrue(kinematics.check()["passed"])
        targets = {}
        for arm, robot in kinematics.robots.items():
            pose = env.robot_manager.get_real_endpose(robot)[0]
            targets[arm] = {"position": pose[:3].tolist(), "quaternion_wxyz": pose[3:].tolist(),
                            "gripper_closed": arm == "left"}
        proposal = kinematics.target(targets)
        action = np.asarray(proposal["action"])
        self.assertEqual(action.shape, (14,))
        # Holding the measured pose keeps the joints; closed is 0 and open is 1.
        np.testing.assert_allclose(action[0:6], env.robot_manager.state["left"]["joints"], atol=1e-5)
        np.testing.assert_allclose(action[7:13], env.robot_manager.state["right"]["joints"], atol=1e-5)
        self.assertEqual((action[6], action[13]), (0.0, 1.0))
        for changed in (
            {"gripper_closed": 1}, {"quaternion_wxyz": [2.0, 0.0, 0.0, 0.0]},
            {"position": [0.0, float("nan"), 0.0]},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                kinematics.target({**targets, "left": {**targets["left"], **changed}})
        with self.assertRaises(ValueError):
            kinematics.target({"left": targets["left"]})

    def test_mismatch_between_urdf_and_measured_pose_fails_closed(self):
        env = type("Env", (), {"robot_manager": Manager(self.urdf, offset=0.01)})()
        with self.assertRaisesRegex(ValueError, "do not match"):
            DualKinematics(env).check()


if __name__ == "__main__":
    unittest.main()
