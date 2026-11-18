"""Simulator-side control loop of one Direct episode.

``run_episode`` is called by the native evaluation loop through the policy's
``deploy.eval_one_episode`` hook, exactly where a learned policy would run.
Native code has already reset the layout and registered the task's completion
conditions; it scores the episode and writes ``_result.json`` after this
function returns. This module never reads rewards or object poses.

Physics advances only inside ``env.take_action``. While the model is thinking
the loop sleeps between short polls, so no control step is spent on waiting.
"""

from __future__ import annotations

from collections.abc import Callable
import time
from typing import Any

from .contract import validate_decision

POLL_SECONDS = 0.2
HARBOR_KEY = "harbor_direct"


class DirectAbort(SystemExit):
    """Stop the native evaluation process.

    A regular exception would be caught by the native loop, which would then
    continue with another layout. An aborted or uncertain run must instead end
    without a complete native result.
    """


def native_conditions(env) -> dict[str, Any]:
    """Fail closed if the native completion conditions were not registered.

    With no registered condition the native reward manager would report an
    immediate success.
    """
    names = ("check_list", "final_check_list", "trigger_check_list")
    counts = {name: [len(group) for group in getattr(env.reward_manager, name)] for name in names}
    if sum(counts[name][0] for name in names) <= 0:
        raise DirectAbort("Native task completion conditions are empty; refusing a vacuous success")
    return {"condition_group_counts": counts}


def joint_action(values) -> dict[str, Any]:
    """Split 14 values into the native dual-arm joint action dictionary."""
    import numpy as np

    action = np.asarray(values, np.float32)
    if action.shape != (14,) or not np.isfinite(action).all():
        raise DirectAbort("Inverse kinematics produced an invalid joint action")
    if not ((action[[6, 13]] >= 0) & (action[[6, 13]] <= 1)).all():
        raise DirectAbort("Inverse kinematics produced an invalid gripper opening")
    return {
        "left_arm_joint_state": action[0:6],
        "left_ee_joint_state": action[6:7],
        "right_arm_joint_state": action[7:13],
        "right_ee_joint_state": action[13:14],
    }


def run_episode(
    env, call: Callable[..., Any], *, kinematics_factory: Callable[[Any], Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Drive one native episode from decisions supplied through ``call``.

    ``call(func_name, obs=None)`` is the native policy client's ``call``.
    """
    if kinematics_factory is None:
        from .kinematics import DualKinematics as kinematics_factory

    def rpc(name: str, payload: dict[str, Any] | None = None) -> Any:
        # A broken policy channel leaves the run in an unknown state.
        try:
            return call(func_name=name) if payload is None else call(func_name=name, obs=payload)
        except Exception as error:
            raise DirectAbort(f"Policy channel failed during {name}: {error}") from error

    rpc("reset")
    if env.num_envs != 1:
        raise DirectAbort("Direct mode evaluates exactly one environment at a time")
    try:
        kinematics = kinematics_factory(env)
        check = kinematics.check()
    except ValueError as error:
        raise DirectAbort(f"Kinematics validation failed: {error}") from error
    max_steps = int(env.step_lim)
    episode_id = rpc("direct_begin", {
        "variant": env.task_name,
        "layout_id": int(env.env_seeds[0]),
        "layout_seed_group": int(env.eval_seed),
        "max_episode_steps": max_steps,
        "control_dt": 1.0 / float(env.obs_manager.collect_freq),
        "kinematics_check": check,
        "native_conditions": native_conditions(env),
    })["episode_id"]
    try:
        previous = _control_loop(env, rpc, kinematics, episode_id, max_steps, sleep)
    except DirectAbort:
        raise
    except BaseException as error:
        # Native code raised, for example on a physics fault. Native recovery
        # decides what happens to this layout; the ledger only records that the
        # episode did not reach native termination, so it can never be scored.
        try:
            call(func_name="direct_abandon", obs={
                "episode_id": episode_id, "error": f"{type(error).__name__}: {error}"[:2000],
            })
        except Exception:
            pass
        raise
    rpc("direct_end", {
        "episode_id": episode_id,
        "control_steps": int(env.take_action_cnt[0]),
        "native_success_observed": bool(env.success[0]),
        "previous_result": previous,
    })


def _control_loop(env, rpc, kinematics, episode_id: str, max_steps: int, sleep) -> dict[str, Any] | None:
    previous: dict[str, Any] | None = None
    while not env.is_episode_end():
        # The native observation travels unchanged; the policy server decodes
        # camera frames the same way it does for learned policies.
        native = dict(env.get_obs())
        step_id = int(env.take_action_cnt[0])
        native[HARBOR_KEY] = {
            "episode_id": episode_id, "step_id": step_id,
            "max_episode_steps": max_steps, "previous_result": previous,
        }
        posted = rpc("direct_observe", native)
        request = {"request_id": posted["request_id"], "current_eef": posted["current_eef"]}
        while True:
            answer = rpc("direct_poll", {"request_id": request["request_id"]})
            if answer["status"] != "pending":
                break
            sleep(POLL_SECONDS)
        if answer["status"] != "decision":
            raise DirectAbort(f"Run aborted by the agent side: {answer.get('reason')}")
        # The broker already validated this decision; check again next to the
        # simulator so that no unbounded target can reach the robot.
        try:
            decision = validate_decision(answer["response"], request)
        except ValueError as error:
            raise DirectAbort(f"Broker delivered an invalid decision: {error}") from error

        diagnostics = []
        for index in range(decision["steps"]):
            try:
                proposal = kinematics.target(decision["target"])
            except ValueError as error:
                raise DirectAbort(f"Inverse kinematics failed: {error}") from error
            before = int(env.take_action_cnt[0])
            env.take_action(joint_action(proposal["action"]))
            if int(env.take_action_cnt[0]) != before + 1:
                raise DirectAbort("Native control step was not executed exactly once")
            diagnostics.append(proposal["diagnostics"])
            if env.is_episode_end() or index + 1 == decision["steps"]:
                break
        previous = {
            "request_id": request["request_id"],
            "requested_steps": decision["steps"],
            "executed_steps": len(diagnostics),
            "start_step_id": step_id,
            "end_step_id": int(env.take_action_cnt[0]),
            "ik_diagnostics": diagnostics,
        }
    return previous
