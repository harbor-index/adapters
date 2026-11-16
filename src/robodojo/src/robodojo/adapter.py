"""Generate pinned RoboDojo simulation tasks without importing Isaac Sim."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
from string import Template
import tempfile

from .catalog import DIMENSION_TASKS, UPSTREAM_REVISION, task_spec

DEFAULT_IMAGE = "robodojo-harbor:f57e1633ffed"


class RoboDojoAdapter:
    """Package native simulation evaluations of an external XPolicyLab policy."""

    def __init__(
        self,
        output_dir: Path,
        *,
        assets_dir: Path,
        image: str = DEFAULT_IMAGE,
        seed: int = 0,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
    ) -> None:
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.assets_dir = Path(assets_dir).expanduser().resolve()
        if not self.assets_dir.is_dir():
            raise ValueError(f"Assets directory does not exist: {self.assets_dir}")
        if any(c in str(self.assets_dir) for c in ("$", "\n", "\r")):
            raise ValueError("Assets path cannot contain Compose interpolation or newlines")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]*", image):
            raise ValueError("Invalid Docker image reference")
        if type(seed) is not int or seed < 0:
            raise ValueError("seed must be a nonnegative integer")
        if limit is not None and (type(limit) is not int or limit <= 0):
            raise ValueError("limit must be positive")
        self.image = image
        self.seed = seed
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.package_dir = Path(__file__).parent

    def run(self) -> list[Path]:
        names = sorted(name for tasks in DIMENSION_TASKS.values() for name in tasks)
        if self.task_ids is not None:
            unknown = set(self.task_ids) - set(names)
            if unknown:
                raise ValueError(f"Unknown canonical task IDs: {', '.join(sorted(unknown))}")
            names = [name for name in names if name in self.task_ids]
        if self.limit is not None:
            names = names[:self.limit]
        targets = [self.output_dir / name.lower().replace("_", "-") for name in names]
        for target in targets:
            if target.is_symlink():
                raise ValueError(f"Refusing to replace a symbolic link: {target}")
            if target.exists() and not self.overwrite:
                raise FileExistsError(f"Task exists: {target}; use --overwrite to replace it")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for name, target in zip(names, targets, strict=True):
            spec = task_spec(name, self.seed)
            with tempfile.TemporaryDirectory(dir=self.output_dir, prefix=".robodojo-") as work:
                staging = Path(work) / "task"
                shutil.copytree(self.package_dir / "task-template", staging)
                values = {
                    "task_id": name,
                    "harbor_name": f"robodojo/{target.name}",
                    "dimension": spec["dimension"],
                    "seed": str(self.seed),
                    "upstream_revision": UPSTREAM_REVISION,
                    "image": self.image,
                }
                for relative in ("task.toml", "instruction.md", "environment/Dockerfile"):
                    file = staging / relative
                    file.write_text(Template(file.read_text()).substitute(values))
                tests = staging / "tests"
                (tests / "task_spec.json").write_text(json.dumps(spec, indent=2) + "\n")
                runtime_package = tests / "robodojo"
                runtime_package.mkdir()
                for module in ("__init__.py", "catalog.py", "runtime.py", "UPSTREAM_LICENSE"):
                    shutil.copy2(self.package_dir / module, runtime_package / module)
                self._write_compose(staging / "environment/docker-compose.yaml")
                for script in staging.rglob("*.sh"):
                    script.chmod(0o755)
                if target.exists():
                    shutil.rmtree(target)
                staging.rename(target)
        return targets

    def _write_compose(self, path: Path) -> None:
        mounts = [{
            "type": "bind", "source": str(self.assets_dir),
            "target": "/workspace/RoboDojo/Assets", "read_only": True,
            "bind": {"create_host_path": False},
        }]
        if str(self.assets_dir) != "/workspace/RoboDojo/Assets":
            mounts.append({**mounts[0], "target": str(self.assets_dir)})
        # JSON is valid YAML, and preserves spaces and punctuation in bind paths.
        compose = {"services": {"main": {
            "platform": "linux/amd64",
            "shm_size": "16gb",
            "volumes": mounts,
            "deploy": {"resources": {"reservations": {"devices": [{
                "driver": "nvidia", "count": 1, "capabilities": ["gpu"],
            }]}}},
        }}}
        path.write_text(json.dumps(compose, indent=2) + "\n")
