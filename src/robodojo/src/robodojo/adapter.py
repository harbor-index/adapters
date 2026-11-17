"""Generate pinned RoboDojo simulation tasks without importing Isaac Sim."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
from string import Template
import tempfile

from .catalog import DIMENSION_TASKS, MOCK_TASK_ID, UPSTREAM_REVISION, mock_task_spec, task_spec

DEFAULT_IMAGE = "robodojo-harbor:f57e1633ffed"
# Limits of the simulator service. Harbor applies the [environment] resources of
# task.toml to the agent container only, so these are set in the Compose file.
# The values are provisional: no run has been measured yet.
SIMULATOR_CPUS = 8
SIMULATOR_MEMORY_MB = 65536
SIMULATOR_STORAGE_MB = 307200
SIMULATOR_GPUS = 1
# Package files copied into each generated task, by the container that runs them.
SIMULATOR_MODULES = (
    "__init__.py", "catalog.py", "runtime.py", "UPSTREAM_LICENSE",
    "direct/__init__.py", "direct/contract.py", "direct/broker.py", "direct/episode.py",
    "direct/kinematics.py", "direct/supervisor.py", "direct/GPT_AS_POLICY_LICENSE",
)
VERIFIER_MODULES = ("__init__.py", "catalog.py", "runtime.py", "verifier.py", "UPSTREAM_LICENSE")
# The CPU mock replaces the supervisor and the simulator; it ships no upstream code.
MOCK_SIMULATOR_MODULES = (
    "__init__.py", "catalog.py", "runtime.py",
    "direct/__init__.py", "direct/contract.py", "direct/broker.py", "direct/episode.py",
    # Only the policy model of the native policy package: it hosts the broker.
    "direct/policy/__init__.py", "direct/policy/model.py",
    "mock/__init__.py", "mock/scene.py", "mock/service.py",
)
MOCK_HARBOR_NAME = "robodojo-mock/reach-targets"


class RoboDojoAdapter:
    """Package native RoboDojo simulation tasks for model control in Direct mode."""

    def __init__(
        self,
        output_dir: Path,
        *,
        assets_dir: Path | None = None,
        image: str = DEFAULT_IMAGE,
        seed: int = 0,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        mock: bool = False,
        mock_episodes: int = 2,
    ) -> None:
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.mock = mock
        if mock:
            # The mock task needs no assets, no simulator image and no GPU.
            if task_ids is not None or limit is not None:
                raise ValueError("The mock is a single task; task_ids and limit do not apply")
            self.assets_dir = None
        else:
            if assets_dir is None:
                raise ValueError("An assets directory is required for the simulator tasks")
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
        # Validates the episode count; only used for the mock task.
        self.mock_spec = mock_task_spec(seed, mock_episodes) if mock else None
        self.image = image
        self.seed = seed
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.package_dir = Path(__file__).parent

    def run(self) -> list[Path]:
        if self.mock:
            names = [MOCK_TASK_ID]
        else:
            names = sorted(name for tasks in DIMENSION_TASKS.values() for name in tasks)
            if self.task_ids is not None:
                unknown = set(self.task_ids) - set(names)
                if unknown:
                    raise ValueError(f"Unknown canonical task IDs: {', '.join(sorted(unknown))}")
                names = [name for name in names if name in self.task_ids]
            if self.limit is not None:
                names = names[:self.limit]
        targets = [self.output_dir / name.lower().replace("_", "-") for name in names]
        # Every target is checked before the first task is written.
        for target in targets:
            if target.is_symlink():
                raise ValueError(f"Refusing to replace a symbolic link: {target}")
            if target.exists() and not self.overwrite:
                raise FileExistsError(f"Task exists: {target}; use --overwrite to replace it")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for name, target in zip(names, targets, strict=True):
            # A task is built in a staging directory and published in one rename.
            with tempfile.TemporaryDirectory(dir=self.output_dir, prefix=".robodojo-") as work:
                staging = Path(work) / "task"
                shutil.copytree(self.package_dir / "task-template", staging)
                if self.mock:
                    self._fill_mock(staging)
                else:
                    self._fill_native(staging, name, f"robodojo/{target.name}")
                for script in staging.rglob("*.sh"):
                    script.chmod(0o755)
                if target.exists():
                    shutil.rmtree(target)
                staging.rename(target)
        return targets

    def _fill_native(self, staging: Path, name: str, harbor_name: str) -> None:
        """One canonical task: the pinned GPU simulator with the native policy."""
        spec = task_spec(name, self.seed)
        self._render(staging, ("task.toml", "instruction.md", "environment/simulator.Dockerfile"), {
            "task_id": name,
            "harbor_name": harbor_name,
            "dimension": spec["dimension"],
            "seed": str(self.seed),
            "upstream_revision": UPSTREAM_REVISION,
            "image": self.image,
            "simulator_cpus": str(SIMULATOR_CPUS),
            "simulator_memory_mb": str(SIMULATOR_MEMORY_MB),
            "simulator_storage_mb": str(SIMULATOR_STORAGE_MB),
            "simulator_gpus": str(SIMULATOR_GPUS),
        })
        # Simulator container: native policy, broker and supervisor.
        self._install(staging, spec, SIMULATOR_MODULES)
        environment = staging / "environment"
        shutil.copytree(
            self.package_dir / "direct" / "policy",
            environment / "simulator" / "robodojo" / "direct" / "policy",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        self._write_compose(environment / "docker-compose.yaml")

    def _fill_mock(self, staging: Path) -> None:
        """The CPU mock task: same agent, bridge and verifier, invented scene."""
        spec = self.mock_spec
        # The mock keeps the agent container and the verifier files of the
        # real template and replaces everything that names the benchmark.
        shutil.copytree(self.package_dir / "mock-template", staging, dirs_exist_ok=True)
        self._render(staging, ("task.toml", "instruction.md"), {
            "harbor_name": MOCK_HARBOR_NAME, "seed": str(spec["seed"]),
            "episodes": str(spec["variants"][0]["episodes"]),
        })
        self._install(staging, spec, MOCK_SIMULATOR_MODULES)
        # The scripted player is the oracle; it runs in the agent container.
        shutil.copy2(self.package_dir / "mock" / "solver.py", staging / "solution" / "solve.py")
        # No GPU, no assets and no fixed platform: this runs on any Docker host.
        compose = {"services": {"simulator": {
            "build": {"context": "${CONTEXT_DIR}", "dockerfile": "simulator.Dockerfile"},
            "cpus": 2.0, "mem_limit": "2048m",
        }}}
        (staging / "environment" / "docker-compose.yaml").write_text(json.dumps(compose, indent=2) + "\n")

    @staticmethod
    def _render(staging: Path, files: tuple[str, ...], values: dict[str, str]) -> None:
        for relative in files:
            file = staging / relative
            file.write_text(Template(file.read_text()).substitute(values))

    def _install(self, staging: Path, spec: dict, simulator_modules: tuple[str, ...]) -> None:
        """Give each container its own files and nothing else."""
        spec_text = json.dumps(spec, indent=2) + "\n"
        environment, tests = staging / "environment", staging / "tests"
        # Agent container: the bridge client only.
        shutil.copy2(self.package_dir / "direct" / "bridge_client.py", environment)
        # Simulator container: the modules of the real or the mock simulator.
        self._copy_modules(environment / "simulator" / "robodojo", simulator_modules)
        (environment / "simulator" / "task_spec.json").write_text(spec_text)
        # Verifier container: validation of the collected results only.
        self._copy_modules(tests / "robodojo", VERIFIER_MODULES)
        (tests / "task_spec.json").write_text(spec_text)

    def _copy_modules(self, destination: Path, modules: tuple[str, ...]) -> None:
        for module in modules:
            target = destination / module
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.package_dir / module, target)

    def _write_compose(self, path: Path) -> None:
        mounts = [{
            "type": "bind", "source": str(self.assets_dir),
            "target": "/workspace/RoboDojo/Assets", "read_only": True,
            "bind": {"create_host_path": False},
        }]
        # Planner configurations can embed the original absolute asset path.
        if str(self.assets_dir) != "/workspace/RoboDojo/Assets":
            mounts.append({**mounts[0], "target": str(self.assets_dir)})
        # JSON is valid YAML, and preserves spaces and punctuation in bind paths.
        # No port is published: the agent container reaches the bridge on the
        # private Compose network, and only Harbor reads the simulator's files.
        # Harbor adds the small [environment] limits of task.toml to "main"; the
        # simulator carries its own CPU, memory and GPU settings here.
        compose = {"services": {
            "main": {"platform": "linux/amd64"},
            "simulator": {
                "build": {"context": "${CONTEXT_DIR}", "dockerfile": "simulator.Dockerfile"},
                "platform": "linux/amd64",
                "shm_size": "16gb",
                "cpus": float(SIMULATOR_CPUS),
                "mem_limit": f"{SIMULATOR_MEMORY_MB}m",
                "volumes": mounts,
                "deploy": {"resources": {"reservations": {"devices": [{
                    "driver": "nvidia", "count": SIMULATOR_GPUS, "capabilities": ["gpu"],
                }]}}},
            },
        }}
        path.write_text(json.dumps(compose, indent=2) + "\n")
