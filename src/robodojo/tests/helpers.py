"""Fakes shared by the CPU tests: a native environment, kinematics and results."""

from types import SimpleNamespace

from robodojo.catalog import UPSTREAM_REVISION

CAMERAS = ("cam_head", "cam_left_wrist", "cam_right_wrist")


def native_result(episodes=50, successes=10, first_layout=0):
    details = {
        str(index): {
            "layout_id": first_layout + index, "success": index < successes,
            "score": 1.0 if index < successes else 0.25,
        }
        for index in range(episodes)
    }
    return {
        "eval_time": episodes, "success_rate": successes / episodes,
        "score": (successes + (episodes - successes) * 0.25) / episodes * 100,
        "details": details,
    }


def task_spec_dict(task_id="insert_tubes", dimension="precision", seed=0):
    names = [task_id, f"{task_id}_random"] if dimension == "generalization" else [task_id]
    return {
        "task_id": task_id, "dimension": dimension, "seed": seed,
        "upstream_revision": UPSTREAM_REVISION,
        "variants": [{"name": name, "episodes": 50 // len(names)} for name in names],
    }


class FakeEnv:
    """The parts of the native evaluation environment that Direct mode touches."""

    num_envs = 1

    def __init__(self, task_name="insert_tubes", layout_id=0, step_lim=12, succeed_at=None, eval_seed=0):
        import numpy as np

        self.np = np
        self.task_name, self.step_lim, self.eval_seed = task_name, step_lim, eval_seed
        self.succeed_at = succeed_at
        self.env_seeds = [layout_id]
        self.take_action_cnt = [0]
        self.success = [True]
        self.end_flag = [False]
        self.obs_manager = SimpleNamespace(collect_freq=25)
        self.reward_manager = SimpleNamespace(
            check_list=[[object()]], final_check_list=[[]], trigger_check_list=[[]],
        )
        self.pose = {"left": [0.1, 0.2, 0.3, 1.0, 0.0, 0.0, 0.0], "right": [0.1, -0.2, 0.3, 1.0, 0.0, 0.0, 0.0]}
        self.opening = {"left": 1.0, "right": 1.0}
        self.actions = []
        self.raise_on_step = None

    def is_episode_end(self):
        if not self.end_flag[0]:
            if self.succeed_at is not None and self.take_action_cnt[0] >= self.succeed_at:
                self.end_flag[0], self.success[0] = True, True
            elif self.take_action_cnt[0] >= self.step_lim:
                self.end_flag[0], self.success[0] = True, False
        return self.end_flag[0]

    def get_obs(self):
        np = self.np
        state = {}
        for arm in ("left", "right"):
            state[f"{arm}_ee_pose"] = np.array(self.pose[arm], np.float32)
            state[f"{arm}_arm_joint_state"] = np.zeros(6, np.float32)
            state[f"{arm}_ee_joint_state"] = np.array([self.opening[arm]], np.float32)
        frame = np.full((4, 6, 3), self.take_action_cnt[0] % 255, np.uint8)
        return {
            "vision": {camera: {"color": frame.copy()} for camera in CAMERAS},
            "state": state, "instruction": "Insert the tubes into the rack.", "env_idx": 0,
        }

    def take_action(self, action):
        if self.raise_on_step is not None and self.take_action_cnt[0] == self.raise_on_step:
            raise RuntimeError("native physics fault")
        self.actions.append(action)
        self.opening = {"left": float(action["left_ee_joint_state"][0]),
                        "right": float(action["right_ee_joint_state"][0])}
        self.take_action_cnt[0] += 1


class FakeKinematics:
    """Reach the target immediately so that later decisions stay within bounds."""

    def __init__(self, env):
        self.env = env

    def check(self):
        return {"passed": True, "arms": {}}

    def target(self, targets):
        action = []
        for arm in ("left", "right"):
            self.env.pose[arm] = [*targets[arm]["position"], *targets[arm]["quaternion_wxyz"]]
            action.extend([0.0] * 6 + [0.0 if targets[arm]["gripper_closed"] else 1.0])
        return {"action": action, "diagnostics": {"method": "fake"}}


def model_call(model):
    """Dispatch like the native policy client: ``call(func_name, obs=None)``."""
    def call(func_name, obs=None):
        method = getattr(model, func_name)
        return method() if obs is None else method(obs)
    return call


def hold(observation, steps=5, **changes):
    """A valid decision that keeps both arms at their measured poses."""
    target = {
        arm: {
            "position": list(observation["current_eef"][arm]["position"]),
            "quaternion_wxyz": list(observation["current_eef"][arm]["quaternion_wxyz"]),
            "gripper_closed": False,
        }
        for arm in ("left", "right")
    }
    return {
        "request_id": observation["request_id"], "mode": "eef", "steps": steps,
        "reason": "Hold both arms.", "target": target, **changes,
    }
