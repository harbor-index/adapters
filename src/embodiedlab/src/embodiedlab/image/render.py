"""Render the agent workspace the way scripts/trial.py start does (texts in templates.json, taken from trial.py).

The helpers below are copies of scripts/trial.py (_tool_table, _task_card, gripper_note, manipulation_note); the
dataset generator checks that a rendered workspace matches trial.py's own rendering byte for byte.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
T = json.loads((HERE / "templates.json").read_text())
ENVIRONMENT_ID = "env_harbor"
CONTROLLER = os.environ.get("AGENT_CONTROLLER_URL", "http://127.0.0.1:18731")


def _write(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str) if not isinstance(value, str) else value)


def _tool_table(tools: list[dict]) -> str:
    lines = ["| tool | signature | what it does |", "|---|---|---|"]
    for tool in tools:
        description = str(tool.get("description") or "").replace("|", "\\|").replace("\n", " ")
        signature = str(tool.get("signature") or "").replace("|", "\\|")
        lines.append(f"| `{tool['name']}` | `{signature}` | {description} |")
    return "\n".join(lines)


def _task_card(spec: dict) -> str:
    for key in ("task_card", "card", "task_card_markdown"):
        if isinstance(spec.get(key), str):
            return spec[key]
    parts = [f"# {spec.get('task_id', 'task')}"]
    for key in ("objective", "apparatus", "instrument", "measurement_requirements", "model_convention",
                "provisional_tolerances", "submission_fields", "device_calibration"):
        if key in spec:
            value = spec[key]
            parts.append(f"\n## {key}\n")
            parts.append(value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False))
    return "\n".join(parts) + "\n"


def gripper_note(task_spec: dict) -> str:
    mechanism = (task_spec.get("device_calibration") or {}).get("gripper_mechanism") or {}
    if mechanism.get("type") != "contact_conditioned_constrained_gripper":
        return ""
    return ("**Gripper (how this simulation grasps, the same at every exposure level).** "
            + mechanism["description"] + " " + T["FINGER_MOTION"]
            + " The numbers are in `device_calibration.gripper_mechanism`.\n")


def manipulation_note(exposure: str, task_spec: dict) -> str:
    text = T["MANIPULATION_INTRO"][exposure] + "\n" + T["ROBOT_NOTE"]
    gripper = gripper_note(task_spec)
    if gripper:
        text += "\n" + gripper
    return text


def write_workspace(root: str, task_spec: dict, tools: list[dict], *, exp_id: str | None = None,
                    exposure: str | None = None, sim_s: float | None = None, wall_s: float | None = None) -> None:
    exp_id = exp_id or os.environ["EL_EXP_ID"]
    exposure = exposure or os.environ["EL_EXPOSURE"]
    sim_s = sim_s if sim_s is not None else json.loads(os.environ["EL_SIM_S"])
    wall_s = float(wall_s if wall_s is not None else os.environ["EL_WALL_S"])
    seen = task_spec.get("exposure") if isinstance(task_spec, dict) else None
    if seen != exposure:
        raise RuntimeError(f"environment reports exposure {seen!r}, task is {exposure!r}")
    workspace = Path(root)
    (workspace / "images").mkdir(parents=True, exist_ok=True)
    (workspace / "tools").mkdir(exist_ok=True)
    _write(workspace / "env.json", {"environment_id": ENVIRONMENT_ID, "controller": CONTROLLER, "workspace": str(workspace),
                                     "exp_id": exp_id, "exposure": exposure})
    _write(workspace / "task_spec.json", task_spec)
    _write(workspace / "tools.json", tools)
    (workspace / "task_card.md").write_text(_task_card(task_spec))
    (workspace / "README.md").write_text(T["README"].format(
        environment_id=ENVIRONMENT_ID, controller=CONTROLLER, exp_id=exp_id, exposure=exposure,
        exposure_note=T["EXPOSURE_NOTES"][exposure], tool_table=_tool_table(tools), sim_s=sim_s,
        wall_min=int(round(wall_s / 60)), manipulation_note=manipulation_note(exposure, task_spec)))
    shutil.copytree(HERE / "embodiedlab_api", workspace / "embodiedlab_api", dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"))
    wrapper = workspace / "py"
    wrapper.write_text('#!/bin/sh\n# Python bound to this workspace.\n'
                       'export PYTHONPATH=' + str(workspace) + '${PYTHONPATH:+:$PYTHONPATH}\nexec python3 "$@"\n')
    wrapper.chmod(0o755)
    (workspace / "NOTES.md").write_text("# Notes\n")
    for path in [workspace, *workspace.rglob("*")]:   # the agent may run under any uid
        path.chmod(0o777 if path.is_dir() or path == wrapper else 0o666)
