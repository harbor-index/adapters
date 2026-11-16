"""Generate stable arena descriptors from complete native CodeClash configs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import tempfile

from .catalog import ARENAS, config_for, get_arena
from .configuration import apply_profile, load_native_config
from .contract import ARENA_NAMES, UPSTREAM_COMMIT, config_digest, validate_task_config

CODECLASH_REVISION = UPSTREAM_COMMIT
PROFILES = ("smoke", "benchmark")


class CodeClashAdapter:
    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        profile: str = "benchmark",
        config: list[Path] | None = None,
        config_root: Path | None = None,
    ):
        self.output_dir, self.limit, self.overwrite = Path(output_dir), limit, overwrite
        self.task_ids, self.profile = task_ids, profile
        self.config, self.config_root = config or [], config_root

    def run(self) -> list[Path]:
        if self.profile not in PROFILES:
            raise ValueError(f"Unknown profile: {self.profile}")
        if self.limit is not None and self.limit < 0:
            raise ValueError("--limit must be nonnegative")
        if self.task_ids is not None and set(self.task_ids) - set(ARENAS):
            raise ValueError(
                f"Unknown task IDs; supported arena IDs: {', '.join(ARENAS)}"
            )
        custom = {}
        for source in self.config:
            value = load_native_config(source, self.config_root)
            slug = next(
                (
                    k
                    for k, name in ARENA_NAMES.items()
                    if name == value.get("game", {}).get("name")
                ),
                None,
            )
            if slug is None:
                raise ValueError("Native config selects an unregistered arena")
            if slug in custom:
                raise ValueError(
                    "Multiple configurations for one arena cannot count as multiple tasks; generate separate datasets"
                )
            custom[slug] = (Path(source), value)
        candidates = sorted(custom or ARENAS)
        selected = [
            k for k in candidates if self.task_ids is None or k in self.task_ids
        ]
        if custom and self.task_ids and set(self.task_ids) - set(custom):
            raise ValueError("--task-ids includes arenas without a supplied --config")
        if self.limit is not None:
            selected = selected[: self.limit]
        package = Path(__file__).parent
        manifests = {}
        # Validate every selected config and overwrite target before writing any.
        for slug in selected:
            arena = get_arena(slug)
            if slug in custom:
                source, native = custom[slug]
                native = apply_profile(native, self.profile, arena["smoke_sims"])
                origin, source_path = "user_supplied", source.name
                source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            else:
                native = config_for(slug, self.profile)
                origin, source_path = (
                    arena["config_origin"],
                    arena["source_config_path"],
                )
                source_hash = arena["source_config_sha256"]
            sources = list(arena.get("external_sources", []))
            workspace = arena["workspace_source"]
            if "repository" in workspace and workspace not in sources:
                sources.insert(0, workspace)
            if not sources:
                sources = [
                    {
                        "repository": "https://github.com/CodeClash-ai/CodeClash",
                        "commit": UPSTREAM_COMMIT,
                    }
                ]
            manifests[slug] = validate_task_config(
                {
                    "schema_version": 2,
                    "adapter": "codeclash",
                    "arena": slug,
                    "arena_name": arena["name"],
                    "mode": "pvp",
                    "profile": self.profile,
                    "upstream_commit": UPSTREAM_COMMIT,
                    "counting_unit": "arena",
                    "source_config_path": source_path,
                    "source_config_sha256": source_hash,
                    "resolved_native_config": native,
                    "resolved_config_sha256": config_digest(native),
                    "config_origin": origin,
                    "arena_sources": sources,
                }
            )
            target = self.output_dir / slug
            if target.is_symlink():
                raise ValueError(f"Refusing to replace symlink: {target}")
            if target.exists():
                if not self.overwrite:
                    raise FileExistsError(f"Task exists: {target}; use --overwrite")
                marker = target / "environment/task-config.json"
                if (
                    not marker.is_file()
                    or json.loads(marker.read_text()).get("adapter") != "codeclash"
                ):
                    raise ValueError(
                        f"Refusing to overwrite a directory not generated by this adapter: {target}"
                    )
        generated = []
        for slug, manifest in manifests.items():
            target = self.output_dir / slug
            self.output_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix=".codeclash-", dir=self.output_dir
            ) as temporary:
                staged = Path(temporary) / slug
                shutil.copytree(package / "task-template", staged)
                path = staged / "environment/task-config.json"
                path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
                shutil.copyfile(path, staged / "tests/task-config.json")
                for filename in ("evidence.py", "contract.py"):
                    shutil.copyfile(package / filename, staged / "tests" / filename)
                task = staged / "task.toml"
                task.write_text(
                    task.read_text()
                    .replace('name = "codeclash/arena"', f'name = "codeclash/{slug}"')
                    .replace('"arena"', json.dumps(slug))
                )
                instruction = staged / "instruction.md"
                instruction.write_text(
                    instruction.read_text()
                    .replace("{{ARENA_NAME}}", manifest["arena_name"])
                    .replace(
                        "{{PLAYERS}}",
                        ", ".join(
                            p["name"]
                            for p in manifest["resolved_native_config"]["players"]
                        ),
                    )
                )
                if target.exists():
                    shutil.rmtree(target)
                staged.rename(target)
            generated.append(target)
        return generated
