from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
from string import Template
import tempfile
import tomllib

from .sources import HARNESS_REVISION, TASKS_REVISION, SourceCheckouts


TEMPLATES = Path(__file__).with_name("task-template")
RUNTIME_DOCKERFILE = Path(__file__).with_name("runtime") / "Dockerfile"
IMAGE_REFERENCE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:/@-]*")
HOSTNAME = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?")


def _qualified_name(task_id: str) -> str:
    return f"nju-link/{task_id.replace('_', '-')}"


class GameLogicBenchAdapter:
    def __init__(
        self,
        source: SourceCheckouts,
        output_dir: Path,
        *,
        task_ids: list[str],
        runtime_image: str | None = None,
        allowed_hosts: tuple[str, ...] = (),
        reference: str = "proper",
        overwrite: bool = False,
    ) -> None:
        if runtime_image is not None and not IMAGE_REFERENCE.fullmatch(runtime_image):
            raise ValueError(f"Invalid runtime image reference: {runtime_image}")
        if any(not HOSTNAME.fullmatch(host) for host in allowed_hosts):
            raise ValueError("--allow-host expects a hostname or IPv4 address")
        self.source = source
        self.output_dir = output_dir.resolve()
        for checkout in [source.harness, source.tasks]:
            if self.output_dir.is_relative_to(checkout):
                raise ValueError("Generate tasks outside the source checkouts")
        self.task_ids = task_ids
        self.runtime_stage = (
            f"FROM {runtime_image}" if runtime_image is not None
            else RUNTIME_DOCKERFILE.read_text(encoding="utf-8").rstrip()
        )
        self.allowed_hosts = allowed_hosts
        self.reference = reference
        self.overwrite = overwrite

    def run(self) -> tuple[int, int, int]:
        for task_id in self.task_ids:
            if reason := self.source.unsupported_reason(task_id):
                raise ValueError(f"{task_id} is not supported by isolated verification: {reason}")
        self.output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        generated = skipped = cells = 0
        for task_id in self.task_ids:
            destination = self.output_dir / task_id
            if destination.is_symlink():
                raise ValueError(f"Refusing to use a symbolic link: {destination}")
            if destination.exists():
                if not self.overwrite:
                    skipped += 1
                    continue
                self._check_existing_task(destination, task_id)
            evaluation, agent = self.source.describe(task_id)
            with tempfile.TemporaryDirectory(prefix=".generate-", dir=self.output_dir) as temporary:
                task_dir = Path(temporary) / task_id
                self._write_task(task_dir, task_id, evaluation, agent)
                if destination.is_symlink():
                    raise ValueError(f"Refusing to use a symbolic link: {destination}")
                if destination.exists():
                    if not self.overwrite:
                        skipped += 1
                        continue
                    self._check_existing_task(destination, task_id)
                    previous = Path(temporary) / "previous"
                    destination.rename(previous)
                    try:
                        task_dir.rename(destination)
                    except OSError:
                        previous.rename(destination)
                        raise
                else:
                    task_dir.rename(destination)
            generated += 1
            cells += len(evaluation["cells"])
        return generated, skipped, cells

    @staticmethod
    def _check_existing_task(destination: Path, task_id: str) -> None:
        path = destination / "task.toml"
        if path.is_file():
            config = tomllib.loads(path.read_text(encoding="utf-8"))
            metadata = config.get("metadata", {})
            if (
                metadata.get("upstream_task_id") == task_id
                and config.get("task", {}).get("name") == _qualified_name(task_id)
            ):
                return
        raise ValueError(f"Refusing to overwrite a directory without matching task metadata: {destination}")

    def _write_license_files(self, destination: Path) -> None:
        licenses = {
            "LICENSE": self.source.tasks / "LICENSE",
            "LICENSE-CC-BY-4.0": self.source.tasks / "LICENSE-CC-BY-4.0",
            "NOTICE.md": self.source.tasks / "NOTICE.md",
            "LICENSE-HARNESS": self.source.harness / "LICENSE",
        }
        # Harbor includes the task README in registry packages.
        notices = [
            "# GameLogicBench licensing and attribution\n\n"
            "Source: NJU-LINK. [Paper](https://arxiv.org/abs/2609.21562)"
        ]
        for filename, source in licenses.items():
            shutil.copyfile(source, destination / filename)
            notices.append(f"## {filename}\n\n{source.read_text(encoding='utf-8').rstrip()}")
        (destination / "README.md").write_text("\n\n".join(notices) + "\n", encoding="utf-8")

    def _write_task(self, destination: Path, task_id: str, evaluation: dict, agent: dict) -> None:
        shutil.copytree(
            TEMPLATES, destination,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        destination.chmod(0o700)
        for role in ["environment", "tests"]:
            path = destination / role / "Dockerfile"
            dockerfile = path.read_text(encoding="utf-8")
            marker = "FROM __GLB_RUNTIME_IMAGE__"
            if not dockerfile.startswith(marker + "\n"):
                raise ValueError(f"Missing runtime image placeholder in {role}/Dockerfile")
            path.write_text(
                dockerfile.replace(marker, self.runtime_stage, 1),
                encoding="utf-8",
            )
        # The pinned native GDExtension is shipped only as a Linux x86_64 binary.
        if any(path.suffix == ".gdextension" for path in self.source.files[(task_id, "game")]):
            for role in ["environment", "tests"]:
                (destination / role / "docker-compose.yaml").write_text(
                    "services:\n  main:\n    platform: linux/amd64\n", encoding="utf-8"
                )
        task_config = destination / "task.toml"
        task_config.write_text(
            Template(task_config.read_text(encoding="utf-8")).substitute(
                task_name=_qualified_name(task_id),
                task_id=task_id,
                kind=task_id.split("_", 1)[0],
                harness_revision=HARNESS_REVISION,
                tasks_revision=TASKS_REVISION,
                license_notice=(
                    "contains_non_commercial_game_resources = true"
                    if task_id == "repo_turbofat_piece_phase" else ""
                ),
                solve_timeout=agent["timeout_sec"],
                solve_cpus=agent["cpus"],
                solve_memory_mb=agent["memory_mb"],
                verifier_timeout=len(evaluation["cells"]) * evaluation["timeout"]["wall"] + 120,
                verifier_cpus=evaluation["resources"]["cpus"],
                verifier_memory_mb=evaluation["resources"]["memory_mb"] + 512,
                agent_network_mode="allowlist" if self.allowed_hosts else "no-network",
                allowed_hosts=json.dumps(self.allowed_hosts),
            ),
            encoding="utf-8",
        )
        instruction = self.source.instruction(task_id, evaluation["deliverable_paths"])
        (destination / "instruction.md").write_text(instruction, encoding="utf-8")
        self._write_license_files(destination)
        self.source.copy_group(task_id, "game", destination / "environment/game")
        self.source.copy_group(
            task_id, f"solutions/{self.reference}", destination / "solution/reference"
        )
        private = destination / "tests/private"
        self.source.copy_group(task_id, "game", private / "game")
        self.source.copy_group(task_id, "judge", private / "judge")
        shutil.copyfile(self.source.tasks / task_id / "task.yaml", private / "task.yaml")
        (private / "evaluation.json").write_text(
            json.dumps(evaluation, indent=2) + "\n", encoding="utf-8"
        )
        bridges = {
            "repo_dothop_move_engine": ("dothop.gd",),
            "repo_abyssal_enemy_engine": ("abyssal_enemy.py",),
            "repo_factory_grid_lifeline": (
                "factory_grid.py", "factory_grid_worker.gd.inc", "factory_grid_judge.gd.inc",
            ),
            "repo_ftl_weapon_charge": ("ftl_weapons.py",),
            "repo_euwar_conquest_engine": (
                "euwar_prepare.py", "euwar_protocol.gd", "euwar_proxy.gd", "euwar_worker.gd",
            ),
            "repo_amsg_movement_engine": ("amsg_movement.py",),
            "repo_openrpg_battle_ai": (
                "openrpg_battle.py", "openrpg_battle_worker.gd.inc",
                "openrpg_battle_observe.gd.inc", "openrpg_battle_judge.gd.inc",
            ),
            "repo_openrpg_status_engine": ("openrpg_status.py",),
            "repo_slayrobot_action_stack": ("slayrobot_combat.py",),
            "repo_slayrobot_deck_ledger": ("slayrobot_deck.py", "slayrobot_combat.py"),
            "repo_slayrobot_interceptor_chain": ("slayrobot_combat.py",),
            "repo_turbofat_piece_phase": ("turbofat_piece.py",),
            "repo_valley_day_engine": ("valley_day.py",),
            "repo_wwii_battle_doctrine": ("wwii_doctrine.py",),
            "atom_light_occlusion": ("light_capture.py", "light_snapshot.gd.inc", "light_worker.gd"),
            "repo_rts_engagement_chain": (
                "rts_engagement.py", "rts_engagement_worker.gd.inc", "rts_engagement_judge.gd.inc",
            ),
            "repo_youtd2_attack_cycle": (
                "youtd2_attacks.py", "youtd2_attacks_worker.gd.inc", "youtd2_attacks_judge.gd.inc",
            ),
            "repo_youtd2_buff_aura_engine": (
                "youtd2_auras.py", "youtd2_auras_worker.gd.inc", "youtd2_auras_judge.gd.inc",
            ),
            "repo_youtd2_projectile_engine": (
                "youtd2_projectiles.py", "youtd2_projectiles_worker.gd.inc", "youtd2_projectiles_judge.gd.inc",
            ),
            "repo_rts_harvest_engine": (
                "rts_harvest.py", "rts_harvest_worker.gd.inc", "rts_harvest_judge.gd.inc",
            ),
        }.get(task_id, ())
        if bridges:
            for name in bridges:
                shutil.copyfile(TEMPLATES.parent / "task-bridges" / name, destination / "tests" / name)
            with (destination / "tests/Dockerfile").open("a", encoding="utf-8") as dockerfile:
                dockerfile.write(f"COPY {' '.join(bridges)} /tests/\n")
        for script in ["solution/solve.sh", "tests/test.sh"]:
            (destination / script).chmod(0o755)
