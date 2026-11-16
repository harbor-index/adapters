"""Load native YAML configs without importing the colliding upstream package."""

from __future__ import annotations

import copy
from pathlib import Path
import re

import yaml


def load_native_config(path: Path, config_root: Path | None = None) -> dict:
    path = Path(path).resolve()
    if config_root is None:
        config_root = next(
            (p for p in path.parents if p.name == "configs"), path.parent
        )
    root = Path(config_root).resolve()
    content = path.read_text()
    pattern = r"^(\s*)(.*?)!include\s+(.+)$"
    # Text expansion intentionally matches pinned upstream resolve_includes,
    # including YAML merge keys; additionally bound paths and recursion.
    seen = set()
    for _ in range(64):
        if not re.search(pattern, content, re.MULTILINE):
            value = yaml.safe_load(content)
            if not isinstance(value, dict):
                raise ValueError("Native config must be a YAML/JSON object")
            return value
        if content in seen:
            raise ValueError("Cyclic native config includes")
        seen.add(content)

        def replace(match):
            indentation, prefix, relative = match.groups()
            target = (root / relative).resolve()
            if not target.is_relative_to(root):
                raise ValueError("Native include escapes config root")
            included = target.read_text().strip()
            if prefix.strip() == "<<:":
                padding = indentation + "  " if indentation else "  "
                body = "\n".join(
                    padding + line if line.strip() else line
                    for line in included.splitlines()
                )
                return indentation + "<<:\n" + body
            if prefix.strip().endswith(":"):
                body = "\n".join(
                    indentation + "  " + line if line.strip() else line
                    for line in included.splitlines()
                )
                return indentation + prefix.strip() + "\n" + body
            return indentation + prefix + included

        content = re.sub(pattern, replace, content, flags=re.MULTILINE)
        if len(content) > 2_000_000:
            raise ValueError("Native config include expansion is too large")
    raise ValueError("Native config include depth exceeded")


def apply_profile(config: dict, profile: str, smoke_sims: int) -> dict:
    value = copy.deepcopy(config)
    if profile == "smoke":
        value["tournament"]["rounds"] = 2
        value["game"]["sims_per_round"] = smoke_sims
        if "sim_concurrency" in value["game"]:
            value["game"]["sim_concurrency"] = 1
        for key in ("sims_per_round", "episodes_per_round"):
            if key in value["game"].get("args", {}):
                value["game"]["args"][key] = smoke_sims
    return value
