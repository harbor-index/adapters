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
DEFAULT_RUNTIME_IMAGE = "gamelogicbench-runtime:godot-4.4"
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
        runtime_image: str = DEFAULT_RUNTIME_IMAGE,
        allowed_hosts: tuple[str, ...] = (),
        reference: str = "proper",
        overwrite: bool = False,
    ) -> None:
        if not IMAGE_REFERENCE.fullmatch(runtime_image):
            raise ValueError(f"Invalid runtime image reference: {runtime_image}")
        if any(not HOSTNAME.fullmatch(host) for host in allowed_hosts):
            raise ValueError("--allow-host expects a hostname or IPv4 address")
        self.source = source
        self.output_dir = output_dir.resolve()
        for checkout in [source.harness, source.tasks]:
            if self.output_dir.is_relative_to(checkout):
                raise ValueError("Generate tasks outside the source checkouts")
        self.task_ids = task_ids
        self.runtime_image = runtime_image
        self.allowed_hosts = allowed_hosts
        self.reference = reference
        self.overwrite = overwrite

    def run(self) -> tuple[int, int, int]:
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

    def _write_task(self, destination: Path, task_id: str, evaluation: dict, agent: dict) -> None:
        shutil.copytree(
            TEMPLATES, destination,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        destination.chmod(0o700)
        for role in ["environment", "tests"]:
            path = destination / role / "Dockerfile"
            dockerfile = path.read_text(encoding="utf-8")
            if "__GLB_RUNTIME_IMAGE__" not in dockerfile:
                raise ValueError(f"Missing runtime image placeholder in {role}/Dockerfile")
            path.write_text(
                dockerfile.replace("__GLB_RUNTIME_IMAGE__", self.runtime_image),
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
        self.source.copy_group(task_id, "game", destination / "environment/game")
        self.source.copy_group(
            task_id, f"solutions/{self.reference}", destination / "solution/reference"
        )
        private = destination / "tests/private"
        self.source.copy_group(task_id, "judge", private / "judge")
        shutil.copyfile(self.source.tasks / task_id / "task.yaml", private / "task.yaml")
        (private / "evaluation.json").write_text(
            json.dumps(evaluation, indent=2) + "\n", encoding="utf-8"
        )
        for script in ["solution/solve.sh", "tests/test.sh"]:
            (destination / script).chmod(0o755)
