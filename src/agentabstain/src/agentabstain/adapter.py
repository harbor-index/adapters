"""AgentAbstain to Harbor task generator."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

SOURCE_COMMIT = "cfc3faf7ab1cfd4892cde1158d6e43b2f312ddc3"
DATASET_REVISION = "842228426c2a703347396501af61c7890972c7ee"
DATASET_URL = "https://huggingface.co/datasets/antiquality/agentabstain"


@dataclass(frozen=True)
class SourceTask:
    category: str
    task_id: str
    task_type: str
    pair_id: str
    task_yaml: dict[str, Any]
    metadata: dict[str, Any]
    initial_states: dict[str, dict[str, Any]]

    @property
    def environments(self) -> tuple[str, ...]:
        values = self.task_yaml.get("environments") or self.metadata.get("environments") or []
        return tuple(str(value) for value in values)

    @property
    def action_type(self) -> str:
        return str(self.metadata.get("action_type") or self.task_yaml.get("action_type") or "")


class AgentAbstainAdapter:
    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        data_dir: Path | None = None,
        split: str = "full",
    ):
        self.output_dir = Path(output_dir)
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.data_dir = Path(data_dir or "data")
        self.split = split
        self.assets_dir = Path(__file__).resolve().parents[2] / "assets"

    def run(self) -> None:
        tasks = self._load_tasks()
        tasks = self._select_split(tasks)
        tasks = self._select_ids(tasks)
        if self.limit is not None:
            if self.limit <= 0:
                raise ValueError("--limit must be a positive integer")
            tasks = tasks[: self.limit]
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for task in tasks:
            self._generate_task(task)
        pair_count = len({task.pair_id for task in tasks})
        print(f"Generated {len(tasks)} tasks ({pair_count} source pairs represented) at {self.output_dir}")

    def _load_tasks(self) -> list[SourceTask]:
        rows_path = self.data_dir / "tasks.jsonl"
        if not rows_path.exists():
            raise FileNotFoundError(
                f"AgentAbstain data not found at {self.data_dir}. "
                f"Download {DATASET_URL} or pass --data-dir."
            )
        tasks: list[SourceTask] = []
        for line in rows_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            category = str(row["category"])
            task_id = str(row["task_id"])
            task_type = str(row["task_type"])
            if task_type not in {"act", "abstain"}:
                raise ValueError(f"Unknown task type {task_type!r}")
            task_root = self.data_dir / "tasks" / category / task_id / task_type
            task_yaml = yaml.safe_load((task_root / "task.yaml").read_text(encoding="utf-8")) or {}
            metadata = yaml.safe_load((task_root.parent / "metadata.yaml").read_text(encoding="utf-8")) or {}
            initial_states = {}
            for env_name in row.get("environments") or task_yaml.get("environments") or []:
                state_path = task_root / "initial_states" / f"{env_name}.json"
                initial_states[str(env_name)] = json.loads(state_path.read_text(encoding="utf-8"))
            pair_id = str(row.get("pair_id") or metadata.get("pair_id") or f"{category}/{task_id}")
            tasks.append(SourceTask(category, task_id, task_type, pair_id, task_yaml, metadata, initial_states))
        self._validate_pairs(tasks)
        return tasks

    @staticmethod
    def _validate_pairs(tasks: list[SourceTask]) -> None:
        grouped: dict[str, set[str]] = {}
        seen: set[tuple[str, str]] = set()
        for task in tasks:
            key = (task.pair_id, task.task_type)
            if key in seen:
                raise ValueError(f"Duplicate AgentAbstain task row: {key}")
            seen.add(key)
            grouped.setdefault(task.pair_id, set()).add(task.task_type)
        invalid = {key: value for key, value in grouped.items() if value != {"act", "abstain"}}
        if invalid:
            raise ValueError(f"Invalid paired tasks: {invalid}")

    def _select_split(self, tasks: list[SourceTask]) -> list[SourceTask]:
        if self.split == "full":
            return tasks
        grouped: dict[str, list[SourceTask]] = {}
        for task in tasks:
            grouped.setdefault(task.pair_id, []).append(task)
        pairs = list(grouped.items())
        if self.split == "smoke":
            selected = [item for item in pairs if item[0] == "ambiguous_action_specification/preview_001"]
            if not selected:
                raise ValueError("Pinned smoke pair ambiguous_action_specification/preview_001 is missing")
        elif self.split == "parity":
            selected = self._select_parity_pairs(pairs)
        else:
            raise ValueError(f"Unknown split {self.split!r}")
        selected_ids = {key for key, _ in selected}
        return [task for task in tasks if task.pair_id in selected_ids]

    @staticmethod
    def _select_parity_pairs(pairs: list[tuple[str, list[SourceTask]]]) -> list[tuple[str, list[SourceTask]]]:
        by_category: dict[str, list[tuple[str, list[SourceTask]]]] = {}
        for pair_id, rows in pairs:
            by_category.setdefault(rows[0].category, []).append((pair_id, rows))

        def rank(pair_id: str) -> str:
            return hashlib.sha256(f"agentabstain-parity-42:{pair_id}".encode()).hexdigest()

        selected = []
        for category in sorted(by_category):
            selected.extend(sorted(by_category[category], key=lambda item: rank(item[0]))[:4])
        selected_ids = {key for key, _ in selected}
        covered = {env for _, rows in selected for env in rows[0].environments}
        remaining = [item for item in pairs if item[0] not in selected_ids]
        while len(covered) < 42 and remaining:
            remaining.sort(
                key=lambda item: (
                    -len(set(item[1][0].environments) - covered),
                    rank(item[0]),
                )
            )
            pair = remaining.pop(0)
            selected.append(pair)
            covered.update(pair[1][0].environments)
        return selected

    def _select_ids(self, tasks: list[SourceTask]) -> list[SourceTask]:
        if not self.task_ids:
            return tasks
        wanted = set(self.task_ids)
        grouped: dict[str, list[SourceTask]] = {}
        for task in tasks:
            grouped.setdefault(task.pair_id, []).append(task)
        selected_pairs = set()
        all_known = set()
        for pair_id, rows in grouped.items():
            aliases = {pair_id}
            for task in rows:
                aliases.add(self._neutral_id(task))
                aliases.add(f"{task.category}/{task.task_id}")
                aliases.add(f"{task.category}/{task.task_id}/{task.task_type}")
            all_known.update(aliases)
            if wanted & aliases:
                selected_pairs.add(pair_id)
        missing = wanted - all_known
        if missing:
            raise ValueError(f"Unknown AgentAbstain task IDs: {sorted(missing)}")
        return [task for task in tasks if task.pair_id in selected_pairs]

    @staticmethod
    def _neutral_id(task: SourceTask) -> str:
        variant = "variant-a" if task.task_type == "act" else "variant-b"
        return f"{task.category}__{task.task_id}__{variant}"

    def _generate_task(self, task: SourceTask) -> None:
        neutral_id = self._neutral_id(task)
        target = self.output_dir / neutral_id
        if target.exists():
            if not self.overwrite:
                raise FileExistsError(f"{target} exists. Use --overwrite.")
            shutil.rmtree(target)
        staging = target.with_name(f".{target.name}.staging")
        if staging.exists():
            shutil.rmtree(staging)
        try:
            for path in ("environment", "solution", "tests"):
                (staging / path).mkdir(parents=True)
            self._write_task_toml(staging, task)
            self._write_instruction(staging, task)
            self._write_runtime(staging, task)
            self._write_verifier(staging, task)
            self._write_solution(staging, task)
            staging.rename(target)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise

    def _write_task_toml(self, staging: Path, task: SourceTask) -> None:
        variant = "variant-a" if task.task_type == "act" else "variant-b"
        tags = ["agentic-abstention", "mcp", task.category]
        content = f'''schema_version = "1.4"

# The verifier runs in its own sandbox. These artifacts are the only agent/runtime
# outputs needed by the verifier for response grading and the sidecar commit check.
artifacts = [
  "/logs/agent/final_response.txt",
  {{ source = "/runtime_logs/execution.json", service = "runtime" }},
]

[task]
name = "agentabstain/{task.category}-{task.task_id}-{variant}"
authors = [
  {{ name = "Xun Liu" }},
  {{ name = "Yi Evie Zhang" }},
  {{ name = "Vira Kasprova" }},
  {{ name = "Parisa Rabbani" }},
  {{ name = "Pardis Sadat Zahraei" }},
  {{ name = "Tianyu Zhang" }},
  {{ name = "Ali Ebrahimpour-Boroojeny" }},
  {{ name = "Varun Chandrasekaran" }}
]

[metadata]
difficulty = "medium"
category = "agentic-abstention"
tags = {json.dumps(tags)}

[metadata.harbor.lint]
ignore = ["artifact-log-overlap"]

[agent]
timeout_sec = 1800.0

[verifier]
timeout_sec = 300.0
environment_mode = "separate"

[verifier.env]
OPENAI_API_KEY = "${{OPENAI_API_KEY:-}}"
OPENAI_BASE_URL = "${{OPENAI_BASE_URL:-}}"
AGENTABSTAIN_JUDGE_MODEL = "${{AGENTABSTAIN_JUDGE_MODEL:-}}"
AGENTABSTAIN_JUDGE_STUB = "${{AGENTABSTAIN_JUDGE_STUB:-}}"

[environment]
build_timeout_sec = 900.0
cpus = 2
memory_mb = 4096
storage_mb = 20480
'''
        (staging / "task.toml").write_text(content, encoding="utf-8")

    def _write_instruction(self, staging: Path, task: SourceTask) -> None:
        system_prompt = str(task.task_yaml.get("system_prompt") or "").strip()
        instruction = str(task.task_yaml.get("instruction") or "").strip()
        content = f'''## System Prompt
{system_prompt}

## User Request
{instruction}

## Available Tools
The task environment exposes the task tools through a local bridge.
Use `python /app/agentabstain_cli.py --list-tools` to inspect the tools that are actually available.
Call a tool with `python /app/agentabstain_cli.py --call <tool_name> --params-json '<json object>'`.
A failed call returns `ok: false`; use the returned result when deciding how to continue.
Before finishing, save your complete final response to `/logs/agent/final_response.txt` and give the same response to the user.
'''
        (staging / "instruction.md").write_text(content, encoding="utf-8")

    def _write_runtime(self, staging: Path, task: SourceTask) -> None:
        environment_dir = staging / "environment"
        runtime_root = environment_dir / "abstention_factory"
        (runtime_root / "runtime").mkdir(parents=True)
        (runtime_root / "environments").mkdir(parents=True)
        for filename in ("base.py", "multi.py"):
            shutil.copy2(self.assets_dir / "runtime" / filename, runtime_root / "runtime" / filename)
        for filename in ("NOTICE.txt", "AGENTABSTAIN-LICENSE"):
            shutil.copy2(self.assets_dir / "runtime" / filename, runtime_root / filename)
        (runtime_root / "runtime" / "__init__.py").write_text("", encoding="utf-8")
        (runtime_root / "__init__.py").write_text("", encoding="utf-8")
        (runtime_root / "environments" / "__init__.py").write_text("", encoding="utf-8")

        registry = [
            "from __future__ import annotations",
            "from typing import Type",
            "from abstention_factory.runtime.base import BaseEnvironment",
        ]
        mapping = []
        for env_name in sorted(task.environments):
            source = self.data_dir / "environments" / env_name
            target = runtime_root / "environments" / env_name
            target.mkdir(parents=True)
            for filename in ("__init__.py", "environment.py", "schema.py"):
                shutil.copy2(source / filename, target / filename)
            match = re.search(
                r"^class\s+(\w+Environment)\s*\(",
                (source / "environment.py").read_text(encoding="utf-8"),
                flags=re.MULTILINE,
            )
            if not match:
                raise ValueError(f"Environment class not found for {env_name}")
            class_name = match.group(1)
            registry.append(
                f"from abstention_factory.environments.{env_name}.environment import {class_name}"
            )
            mapping.append(f"    {env_name!r}: {class_name},")
        registry.extend(
            [
                "",
                "ENVIRONMENT_REGISTRY = {",
                *mapping,
                "}",
                "",
                "def get_environment_class(name: str) -> Type[BaseEnvironment]:",
                "    return ENVIRONMENT_REGISTRY[name]",
                "",
            ]
        )
        (runtime_root / "runtime" / "registry.py").write_text("\n".join(registry), encoding="utf-8")

        config = {
            "env_types": list(task.environments),
            "initial_states": task.initial_states,
            "broken_tools": task.task_yaml.get("tool_overrides", {}).get("broken_tools", []),
            "available_tools": task.task_yaml.get("available_tools"),
        }
        (environment_dir / "task_runtime.json").write_text(
            json.dumps(config, indent=2), encoding="utf-8"
        )
        shutil.copy2(self.assets_dir / "runtime_server.py", environment_dir / "runtime_server.py")
        shutil.copy2(self.assets_dir / "agentabstain_cli.py", environment_dir / "agentabstain_cli.py")

        main_dockerfile = '''FROM python:3.11-slim

WORKDIR /app
RUN pip install --no-cache-dir pyyaml==6.0.3
COPY agentabstain_cli.py /app/agentabstain_cli.py
CMD ["sh", "-c", "sleep infinity"]
'''
        (environment_dir / "Dockerfile").write_text(main_dockerfile, encoding="utf-8")

        runtime_dockerfile = '''FROM python:3.11-slim

WORKDIR /app
ENV PYTHONPATH=/app
RUN pip install --no-cache-dir fastmcp==4.2.2
COPY abstention_factory /app/abstention_factory
COPY runtime_server.py /app/runtime_server.py
COPY task_runtime.json /app/task_runtime.json
CMD ["python3", "/app/runtime_server.py"]
'''
        (environment_dir / "runtime.Dockerfile").write_text(runtime_dockerfile, encoding="utf-8")

        compose = '''services:
  main:
    depends_on:
      runtime:
        condition: service_healthy
    environment:
      AGENTABSTAIN_RUNTIME_URL: http://runtime:8000
    volumes:
      - agentabstain_runtime_logs:/runtime_logs:ro

  runtime:
    build:
      context: ${CONTEXT_DIR}
      dockerfile: runtime.Dockerfile
    expose:
      - "8000"
    volumes:
      - agentabstain_runtime_logs:/runtime_logs
    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=2)"]
      interval: 2s
      timeout: 5s
      retries: 15
      start_period: 5s

volumes:
  agentabstain_runtime_logs:
'''
        (environment_dir / "docker-compose.yaml").write_text(compose, encoding="utf-8")

    def _write_verifier(self, staging: Path, task: SourceTask) -> None:
        source_task = dict(task.task_yaml)
        source_task.update(
            category=task.category,
            task_id=task.task_id,
            task_type=task.task_type,
            pair_id=task.pair_id,
        )
        (staging / "tests" / "source_task.yaml").write_text(
            yaml.safe_dump(source_task, sort_keys=False), encoding="utf-8"
        )
        metadata = {
            "pair_id": task.pair_id,
            "category": task.category,
            "task_id": task.task_id,
            "task_type": task.task_type,
            "action_type": task.action_type,
            "environments": list(task.environments),
            "source_commit": SOURCE_COMMIT,
            "dataset_revision": DATASET_REVISION,
        }
        (staging / "tests" / "task_metadata.json").write_text(
            json.dumps(metadata, indent=2), encoding="utf-8"
        )
        shutil.copy2(self.assets_dir / "verifier.py", staging / "tests" / "verifier.py")
        test_sh = '''#!/bin/bash
set -u
rm -f /logs/verifier/reward.txt /logs/verifier/agentabstain_metrics.json
python3 /tests/verifier.py
status=$?
if [ ! -f /logs/verifier/reward.txt ]; then
  echo 0 > /logs/verifier/reward.txt
fi
exit "$status"
'''
        (staging / "tests" / "test.sh").write_text(test_sh, encoding="utf-8")
        (staging / "tests" / "test.sh").chmod(0o755)
        verifier_dockerfile = '''FROM python:3.11-slim

WORKDIR /app
RUN pip install --no-cache-dir pyyaml==6.0.3
RUN mkdir -p /logs/agent /logs/verifier /runtime_logs
COPY . /tests/
'''
        (staging / "tests" / "Dockerfile").write_text(
            verifier_dockerfile, encoding="utf-8"
        )

    def _write_solution(self, staging: Path, task: SourceTask) -> None:
        source_task = dict(task.task_yaml)
        source_task.update(category=task.category, task_id=task.task_id, task_type=task.task_type)
        (staging / "solution" / "source_task.yaml").write_text(
            yaml.safe_dump(source_task, sort_keys=False), encoding="utf-8"
        )
        shutil.copy2(self.assets_dir / "oracle.py", staging / "solution" / "oracle.py")
        solve_sh = '''#!/bin/bash
set -u
python3 /solution/oracle.py
'''
        (staging / "solution" / "solve.sh").write_text(solve_sh, encoding="utf-8")
        (staging / "solution" / "solve.sh").chmod(0o755)
