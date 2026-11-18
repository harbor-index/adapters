"""Generate CWE-Bench tasks with an isolated verifier."""

from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

import yaml


REQUIRED_FILES = (
    "task.toml",
    "instruction.md",
    "environment/Dockerfile",
    "tests/test.sh",
    "solution/solve.sh",
)
BENCHMARK_AUTHORS = (
    'authors = [{ name = "Gonzalo Gonzalez-Pumariega" }, '
    '{ name = "Ajay Jagannath" }, { name = "Adit Jain" }, '
    '{ name = "Guram Gogia" }, { name = "Soumyadeep Bakshi" }, '
    '{ name = "Nazneen Rajani" }]'
)
PATCH_COLLECTION = (
    '[[verifier.collect]]\n'
    'command = "git -C /app add --all && git -C /app diff --cached --binary '
    'cwe-bench-base > /logs/artifacts/model.patch || '
    ': > /logs/artifacts/model.patch"\n'
)
PATCH_APPLICATION = '''# Apply only the submitted /app changes in the clean verifier container.
reject_patch() {
  mkdir -p /logs/verifier
  printf '0\\n' > /logs/verifier/reward.txt
  printf '{"reward":0,"continuous_score":0,"programmatic":0}\\n' > /logs/verifier/reward.json
  exit 0
}
patch=/logs/artifacts/model.patch
if [ ! -f "$patch" ]; then
  echo "[adapter] agent patch artifact was not transferred" >&2
  exit 1
fi
if [ -s "$patch" ]; then
  git -C /app apply --check "$patch" && git -C /app apply "$patch" || reject_patch
fi

'''
GIT_BASELINE = '''# Record a clean source baseline for patch transfer to the separate verifier.
RUN apt-get update && apt-get install -y --no-install-recommends git \\
    && rm -rf /var/lib/apt/lists/*
RUN git init -q && git add -A \\
    && git -c user.email=adapter@localhost -c user.name=adapter \\
       commit -qm "clean CWE-Bench baseline" \\
    && git tag cwe-bench-base
'''
TASK_CONFIG_OVERRIDES = {
    "c-cwe326-608cfffc": (("build_timeout_sec = 600.0", "build_timeout_sec = 2400.0"),),
    "go-cwe295-558d2730": (
        ("memory_mb = 2048", "memory_mb = 8192"),
        ("storage_mb = 4096", "storage_mb = 8192"),
    ),
    "go-cwe613-75fa8fbd": (
        ("memory_mb = 2048", "memory_mb = 8192"),
        ("storage_mb = 4096", "storage_mb = 8192"),
    ),
    "java-cwe522-8914dc0a": (("memory_mb = 2048", "memory_mb = 8192"),),
    "javascript-cwe219-bedf9af6-hardened-v3": (
        ("memory_mb = 2048", "memory_mb = 8192"),
        ("storage_mb = 4096", "storage_mb = 8192"),
    ),
    "javascript-cwe836-0c737699-hardened-v4": (
        ("memory_mb = 2048", "memory_mb = 8192"),
        ("storage_mb = 4096", "storage_mb = 8192"),
    ),
    "rust-cwe328-bd587348-hardened-v1": (
        ("memory_mb = 2048", "memory_mb = 8192"),
        ("storage_mb = 4096", "storage_mb = 8192"),
    ),
    "typescript-cwe306-43fe6624": (("memory_mb = 2048", "memory_mb = 8192"),),
}


class CWEBenchAdapter:
    def __init__(
        self,
        output_dir: Path,
        source_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.source_dir = Path(source_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids

    def run(self) -> list[Path]:
        source_tasks = self.source_dir / "tasks"
        if not source_tasks.is_dir():
            raise FileNotFoundError(f"CWE-Bench tasks directory not found: {source_tasks}")
        if self.limit is not None and self.limit < 0:
            raise ValueError("--limit must be non-negative")

        tasks = sorted(p for p in source_tasks.iterdir() if p.is_dir())
        if self.task_ids:
            requested = set(self.task_ids)
            found = {p.name for p in tasks}
            missing = requested - found
            if missing:
                raise ValueError(f"Unknown task IDs: {', '.join(sorted(missing))}")
            tasks = [p for p in tasks if p.name in requested]
        if self.limit is not None:
            tasks = tasks[: self.limit]

        destinations = [self.output_dir / task.name for task in tasks]
        needs_authors = []
        for source, destination in zip(tasks, destinations):
            for relative_path in REQUIRED_FILES:
                if not (source / relative_path).is_file():
                    raise FileNotFoundError(source / relative_path)
            with (source / "task.toml").open("rb") as config_file:
                config = tomllib.load(config_file)
            task = config.get("task", {})
            if not task.get("name"):
                raise ValueError(f"Missing task name: {source / 'task.toml'}")
            compose_file = source / "environment/docker-compose.yaml"
            compose = yaml.safe_load(compose_file.read_text())
            expected_compose = {
                "services": {
                    "main": {
                        "build": {"context": "."},
                        "image": f"cwe-{source.name}:local",
                        "pull_policy": "build",
                        "working_dir": "/app",
                        "command": ["sh", "-c", "sleep infinity"],
                    }
                }
            }
            dockerfile = (source / "environment/Dockerfile").read_text()
            if compose != expected_compose or not {
                "WORKDIR /app",
                'CMD ["sleep", "infinity"]',
            }.issubset(dockerfile.splitlines()):
                raise ValueError(f"Compose service is not redundant: {compose_file}")
            needs_authors.append(not task.get("authors"))
            if destination.resolve() == source.resolve():
                raise ValueError(f"Output would overwrite the source task: {source}")
            if destination.exists() and not self.overwrite:
                raise FileExistsError(destination)

        self.output_dir.mkdir(parents=True, exist_ok=True)
        for source, destination, add_authors in zip(tasks, destinations, needs_authors):
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(source, destination)
            (destination / "environment/docker-compose.yaml").unlink()
            if add_authors:
                task_file = destination / "task.toml"
                before, separator, after = task_file.read_text().partition("\n[metadata]")
                if not separator:
                    raise ValueError(f"Cannot locate [metadata] after [task]: {task_file}")
                task_file.write_text(before.rstrip() + "\n" + BENCHMARK_AUTHORS + "\n\n[metadata]" + after)
            task_file = destination / "task.toml"
            task_text = task_file.read_text()
            if 'environment_mode' in task_text or '[[verifier.collect]]' in task_text:
                raise ValueError(f"Task already defines verifier isolation: {task_file}")
            task_text = task_text.replace(
                "[verifier]\n",
                '[verifier]\nenvironment_mode = "separate"\n',
                1,
            )
            task_text = task_text.replace("[agent]\n", PATCH_COLLECTION + "\n[agent]\n", 1)
            for original, replacement in TASK_CONFIG_OVERRIDES.get(source.name, ()):
                if task_text.count(original) != 1:
                    raise ValueError(f"Expected one {original!r} in {task_file}")
                task_text = task_text.replace(original, replacement, 1)
            task_file.write_text(task_text)
            dockerfile = destination / "environment/Dockerfile"
            dockerfile.write_text(dockerfile.read_text().replace(
                'CMD ["sleep", "infinity"]\n', GIT_BASELINE + 'CMD ["sleep", "infinity"]\n', 1
            ))
            test_script = destination / "tests/test.sh"
            test_script.write_text(test_script.read_text().replace(
                "#!/usr/bin/env bash\n", "#!/usr/bin/env bash\n" + PATCH_APPLICATION, 1
            ))
            if source.name == "go-cwe273-35cd46d2-hardened-v3":
                verifier = destination / "tests/verify_programmatic.py"
                verifier_text = verifier.read_text()
                original = "DAEMON_IDENTITY = (rng.randrange(64500, 66500), rng.randrange(68000, 69900))"
                replacement = "DAEMON_IDENTITY = (rng.randrange(62500, 64000), rng.randrange(64000, 65535))"
                if verifier_text.count(original) != 1:
                    raise ValueError(f"Expected one identity fixture in {verifier}")
                verifier.write_text(verifier_text.replace(original, replacement, 1))
        return destinations
