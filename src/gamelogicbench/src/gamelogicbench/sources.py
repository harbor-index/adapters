from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path
import re
import shutil
import subprocess

import yaml


HARNESS_REVISION = "979cd919689c8bba021ef56808887bc58e49f3db"
TASKS_REVISION = "fcc0b01fcc2fd2d95998478152222e6ab916fcdf"
TASK_NAME = re.compile(r"^(atom|combo|repo)_[a-z0-9_]+$")
PRESS_TOKEN = re.compile(r"^[a-z][a-z0-9_]*$")
MEMORY = re.compile(r"^(\d+)([mg])$")
WORKSPACE = "/workspace/game"
DEFAULT_DELIVERABLE = "res://logic/controller.gd"
LIVE_OBJECT_CONTROLLERS = {
}


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        encoding="utf-8",
        errors="surrogateescape",
    ).stdout


def verify_checkout(path: Path, revision: str) -> None:
    if Path(_git(path, "rev-parse", "--show-toplevel").strip()) != path:
        raise ValueError(f"Expected the checkout root: {path}")
    actual = _git(path, "rev-parse", "HEAD").strip()
    if actual != revision:
        raise ValueError(f"{path} is at {actual}, expected {revision}")
    if _git(path, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError(f"{path} has changes to tracked files")


def copy_tracked_files(
    source: Path, destination: Path, tracked: list[Path]
) -> None:
    for relative in tracked:
        path = source / relative
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Expected a tracked regular file: {path}")
        output = destination / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, output)
        output.chmod(0o755 if path.stat().st_mode & 0o111 else 0o644)


def memory_mb(value: str) -> int:
    match = MEMORY.fullmatch(value)
    if match is None:
        raise ValueError(f"Unexpected memory unit: {value}")
    count, unit = match.groups()
    return int(count) * (1024 if unit == "g" else 1)


def _press_argument(raw: dict) -> str:
    if not isinstance(raw, dict) or not raw:
        raise ValueError("Expected explicit press axes and tiers")
    pairs = []
    for axis, tier in raw.items():
        for name in tier if isinstance(tier, list) else [tier]:
            if not PRESS_TOKEN.fullmatch(str(axis)):
                raise ValueError(f"Invalid press axis: {axis}")
            if not PRESS_TOKEN.fullmatch(str(name)) or name == "default":
                raise ValueError(f"Invalid press tier: {name}")
            pairs.append(f"{axis}:{name}")
    if not pairs:
        raise ValueError("Expected at least one press tier")
    return ",".join(pairs)


def _cells(raw: dict, kind: str) -> list[dict]:
    scenarios = raw["scenarios"]
    if "baseline" not in scenarios:
        raise ValueError("Missing baseline scenario")
    order = ["baseline", *(name for name in scenarios if name != "baseline")]
    press_enabled = raw.get("press_semantics")
    if press_enabled is None:
        press_enabled = kind == "combo"
    cells = []
    for name in order:
        if not PRESS_TOKEN.fullmatch(name) or re.search(r"_\d+$", name):
            raise ValueError(f"Invalid scenario name: {name}")
        entry = scenarios[name]
        public = name == "baseline"
        press = "" if public or not press_enabled else _press_argument(entry.get("press"))
        seeds = entry["seeds"]
        if not seeds or any(type(seed) is not int for seed in seeds):
            raise ValueError(f"Invalid seeds for {name}")
        if len(seeds) != len(set(seeds)):
            raise ValueError(f"Duplicate seed in {name}")
        cells.extend(
            {
                "scenario": name,
                "seed": seed,
                "press": press,
                "tier": "public" if public else "hidden",
            }
            for seed in seeds
        )
    return cells


def _brief_constants(path: Path) -> tuple[str, str]:
    constants = {}
    for statement in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name) and target.id in {"BRIEF", "BRIEF_NO_NET"}:
                    constants[target.id] = ast.literal_eval(statement.value)
    return constants["BRIEF"], constants["BRIEF_NO_NET"]


class SourceCheckouts:
    def __init__(self, harness: Path, tasks: Path) -> None:
        self.harness = harness.resolve()
        self.tasks = tasks.resolve()
        verify_checkout(self.harness, HARNESS_REVISION)
        verify_checkout(self.tasks, TASKS_REVISION)
        self.config = yaml.safe_load((self.harness / "conf/config.yaml").read_text(encoding="utf-8"))
        self.brief, self.network_note = _brief_constants(self.harness / "geb/pipeline/solve.py")
        self.files: dict[tuple[str, str], list[Path]] = defaultdict(list)
        self.task_ids = []
        for tracked in _git(self.tasks, "ls-files", "-z").split("\0"):
            if not tracked:
                continue
            task_id, *parts = Path(tracked).parts
            if not TASK_NAME.fullmatch(task_id):
                continue
            if parts == ["task.yaml"]:
                self.task_ids.append(task_id)
            elif len(parts) >= 2:
                group = "/".join(parts[:2]) if parts[0] == "solutions" else parts[0]
                subpath = parts[2:] if parts[0] == "solutions" else parts[1:]
                self.files[(task_id, group)].append(Path(*subpath))
        self.task_ids.sort()

    def copy_group(self, task_id: str, group: str, destination: Path) -> None:
        tracked = self.files[(task_id, group)]
        if not tracked:
            raise ValueError(f"{task_id} lacks tracked content in {group}")
        copy_tracked_files(self.tasks / task_id / group, destination, tracked)

    def unsupported_reason(self, task_id: str) -> str | None:
        if task_id in LIVE_OBJECT_CONTROLLERS:
            return LIVE_OBJECT_CONTROLLERS[task_id]
        evaluation, _ = self.describe(task_id)
        if evaluation["mode"] != "headless" and task_id != "atom_light_occlusion":
            return "Rendered project submissions need a separate trusted rendering interface."
        if evaluation["invert_overlay"] and task_id not in {
            "repo_abyssal_enemy_engine", "repo_amsg_movement_engine", "repo_dothop_move_engine",
            "repo_euwar_conquest_engine", "repo_factory_grid_lifeline", "repo_ftl_weapon_charge",
            "repo_openrpg_battle_ai", "repo_openrpg_status_engine",
            "repo_rts_engagement_chain", "repo_rts_harvest_engine", "repo_slayrobot_action_stack", "repo_slayrobot_deck_ledger",
            "repo_slayrobot_interceptor_chain", "repo_turbofat_piece_phase", "repo_valley_day_engine",
            "repo_wwii_battle_doctrine", "repo_youtd2_attack_cycle", "repo_youtd2_buff_aura_engine",
            "repo_youtd2_projectile_engine",
        }:
            return "Project module submissions need an isolated game-object mutation interface."
        return None

    def describe(self, task_id: str) -> tuple[dict, dict]:
        path = self.tasks / task_id / "task.yaml"
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        kind = task_id.split("_", 1)[0]
        if raw["name"] != task_id or raw["kind"] != kind:
            raise ValueError(f"{path}: name or kind does not match its task ID")
        game = self.tasks / task_id / "game"
        for required in ["README.md", "project.godot"]:
            if Path(required) not in self.files[(task_id, "game")]:
                raise ValueError(f"{task_id} lacks game/{required}")
        if Path("judge.tscn") not in self.files[(task_id, "judge")]:
            raise ValueError(f"{task_id} lacks judge/judge.tscn")

        judge = raw.get("judge", {})
        solve = raw.get("solve", {})
        deliverables = judge.get("deliverable_paths") or [DEFAULT_DELIVERABLE]
        for deliverable in deliverables:
            if not deliverable.startswith("res://"):
                raise ValueError(f"{task_id}: invalid deliverable path {deliverable}")
            relative = Path(deliverable.removeprefix("res://"))
            if relative.is_absolute() or ".." in relative.parts or not (game / relative).exists():
                raise ValueError(f"{task_id}: invalid deliverable path {deliverable}")

        judge_defaults = self.config["judge"]
        solve_defaults = self.config["solve"]
        judge_limits = judge_defaults["resources"] | judge.get("resources", {})
        solve_limits = solve_defaults["resources"] | solve.get("resources", {})
        judge_mode = judge.get("mode", "headless")
        if judge_mode not in {"headless", "rendered"}:
            raise ValueError(f"{task_id}: unsupported judge mode {judge_mode}")
        evaluation = {
            "task_id": task_id,
            "cells": _cells(raw, kind),
            "deliverable_paths": deliverables,
            "invert_overlay": bool(judge.get("deliverable_paths")),
            "mode": judge_mode,
            "window": {"width": 640, "height": 480} | judge.get("window", {}),
            "timeout": judge_defaults["timeout"] | judge.get("timeout", {}),
            "resources": {
                "cpus": judge_limits["cpus"],
                "memory_mb": memory_mb(judge_limits["memory"]),
                "pids_limit": judge_limits["pids_limit"],
            },
            "render_threads": self.config["record"]["lp_num_threads"],
        }
        agent = {
            "cpus": solve_limits["cpus"],
            "memory_mb": memory_mb(solve_limits["memory"]),
            "timeout_sec": (solve_defaults["timeout"] | solve.get("timeout", {}))["container"],
        }
        return evaluation, agent

    def instruction(self, task_id: str, deliverables: list[str]) -> str:
        primary = deliverables[0]
        relative = [path.removeprefix("res://") for path in deliverables]
        game = set(self.files[(task_id, "game")])
        entry_scene = "main.tscn"
        if Path("main.tscn") not in game and Path("preview.tscn") in game:
            entry_scene = "preview.tscn"
        return self.brief.format(
            ws=WORKSPACE,
            deliverable=" and ".join(deliverables),
            helper_dir=primary.rsplit("/", 1)[0] + "/",
            deliverable_base=primary.rsplit("/", 1)[1],
            deliverable_rel=" and ".join([relative[0], *(f"{WORKSPACE}/{p}" for p in relative[1:])]),
            entry_scene=f"res://{entry_scene}",
        ) + self.network_note.format(ws=WORKSPACE)
