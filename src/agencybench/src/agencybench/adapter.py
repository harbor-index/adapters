"""AgencyBench V2 -> Harbor task generator."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

from .oracles import render_solve_sh
from .registry import (
    SCENARIO_BY_TASK_ID,
    UPSTREAM_COMMIT,
    UPSTREAM_REPO_URL,
    ScenarioSpec,
    select_scenarios,
)
from .sandbox_io import safe_copy_full

TEMPLATE_DIR = Path(__file__).resolve().parent / "task-template"
VERIFIER_MODULE_PATH = Path(__file__).resolve().parent / "verifier.py"


class AgencyBenchAdapter:
    """Generates Harbor tasks from pinned GAIR-NLP/AgencyBench V2 scenarios."""

    def __init__(
        self,
        output_dir: Path,
        limit: int | None = None,
        overwrite: bool = False,
        task_ids: list[str] | None = None,
        split: str = "implemented",
        upstream_dir: Path | None = None,
        **kwargs,
    ) -> None:
        self.output_dir = Path(output_dir).resolve()
        self.limit = limit
        self.overwrite = overwrite
        self.task_ids = task_ids
        self.split = split
        self.upstream_dir = Path(upstream_dir).resolve() if upstream_dir else None

    def _resolve_upstream_dir(self) -> Path:
        if self.upstream_dir is not None:
            if not (self.upstream_dir / "AgencyBench-v2").is_dir():
                raise FileNotFoundError(
                    f"AgencyBench-v2 not found under --upstream-dir {self.upstream_dir}"
                )
            return self.upstream_dir

        env_override = os.environ.get("AGENCYBENCH_UPSTREAM_ROOT")
        if env_override:
            cand = Path(env_override).resolve()
            if (cand / "AgencyBench-v2").is_dir():
                return cand

        # Check sibling upstream checkout if present
        local_candidate = Path(__file__).resolve().parents[5] / "upstream" / "AgencyBench"
        if (local_candidate / "AgencyBench-v2").is_dir():
            return local_candidate

        # Fallback: clone pinned revision into cache outside adapter source tree
        cache_dir = Path.home() / ".cache" / "harbor-agencybench" / UPSTREAM_COMMIT
        if not (cache_dir / "AgencyBench-v2").is_dir():
            cache_dir.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "clone", "--depth", "1", UPSTREAM_REPO_URL, str(cache_dir)],
                check=True,
            )
            fetch_res = subprocess.run(
                ["git", "-C", str(cache_dir), "fetch", "--depth", "1", "origin", UPSTREAM_COMMIT],
                check=False,
            )
            if fetch_res.returncode != 0:
                subprocess.run(
                    ["git", "-C", str(cache_dir), "fetch", "origin"],
                    check=True,
                )
            subprocess.run(
                ["git", "-C", str(cache_dir), "checkout", "--detach", UPSTREAM_COMMIT],
                check=True,
            )
            head = subprocess.check_output(
                ["git", "-C", str(cache_dir), "rev-parse", "HEAD"],
                text=True,
            ).strip()
            if head != UPSTREAM_COMMIT:
                raise RuntimeError(
                    f"Pinned AgencyBench commit mismatch: expected {UPSTREAM_COMMIT}, got {head}"
                )
        return cache_dir

    def run(self) -> list[Path]:
        upstream_root = self._resolve_upstream_dir()
        scenarios = select_scenarios(
            split=self.split,
            task_ids=self.task_ids,
            limit=self.limit,
        )
        self.output_dir.mkdir(parents=True, exist_ok=True)

        generated: list[Path] = []
        for spec in scenarios:
            if not spec.implemented:
                raise NotImplementedError(
                    f"Scenario {spec.scenario_id} ({spec.task_id}) is not yet implemented "
                    f"(external requirements: {list(spec.external_requirements)}). "
                    "Use --split pilot or --split implemented."
                )
            task_dir = self._generate_task(spec, upstream_root)
            generated.append(task_dir)
        return generated

    def _generate_task(self, spec: ScenarioSpec, upstream_root: Path) -> Path:
        task_dir = self.output_dir / spec.task_id
        if task_dir.exists():
            if not self.overwrite:
                return task_dir
            shutil.rmtree(task_dir)

        upstream_scenario = upstream_root / spec.upstream_rel_path
        desc_path = upstream_scenario / "description.json"
        eval_path = upstream_scenario / "eval_task.py"
        if not desc_path.is_file() or not eval_path.is_file():
            raise FileNotFoundError(f"Missing upstream scenario files under {upstream_scenario}")

        description = json.loads(desc_path.read_text(encoding="utf-8"))

        env_dir = task_dir / "environment"
        sol_dir = task_dir / "solution"
        tests_dir = task_dir / "tests"
        upstream_tests_dir = tests_dir / "upstream"
        for d in (env_dir, sol_dir, tests_dir, upstream_tests_dir):
            d.mkdir(parents=True, exist_ok=True)

        # 1. task.toml
        (task_dir / "task.toml").write_text(self._render_task_toml(spec), encoding="utf-8")

        # 2. instruction.md (STRICTLY subtask1 only; never leak future subtask prompts!)
        (task_dir / "instruction.md").write_text(
            self._render_instruction_md(spec, description), encoding="utf-8"
        )

        # 3. environment/Dockerfile + initial agent-visible assets ONLY
        self._populate_environment(spec, upstream_scenario, env_dir)

        # 4. solution/solve.sh (verified stage-by-stage oracle)
        solve_path = sol_dir / "solve.sh"
        solve_path.write_text(render_solve_sh(spec.runner_family, task_id=spec.task_id), encoding="utf-8")
        self._make_executable(solve_path)

        # 5. tests/ (verifier-only: unaltered upstream eval_task.py + description.json + run_verifier.py + test.sh)
        shutil.copy2(eval_path, upstream_tests_dir / "eval_task.py")
        shutil.copy2(desc_path, upstream_tests_dir / "description.json")
        if spec.runner_family == "code_loss_single":
            pinned_ws = upstream_tests_dir / "workspace"
            pinned_ws.mkdir(parents=True, exist_ok=True)
            for fname in ("evaluate_equation.py", "equation.parquet"):
                src_f = upstream_scenario / "workspace" / fname
                if src_f.is_file():
                    shutil.copy2(src_f, pinned_ws / fname)
        shutil.copy2(VERIFIER_MODULE_PATH, tests_dir / "run_verifier.py")

        test_sh = tests_dir / "test.sh"
        test_sh.write_text(self._render_test_sh(spec), encoding="utf-8")
        self._make_executable(test_sh)

        return task_dir

    @staticmethod
    def _make_executable(path: Path) -> None:
        mode = path.stat().st_mode
        path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    @staticmethod
    def _render_task_toml(spec: ScenarioSpec) -> str:
        tags = json.dumps(["agencybench", "agencybench-v2", spec.category.lower(), spec.runner_family])
        return textwrap.dedent(
            f"""\
            version = "1.0"

            [task]
            name = "{spec.registry_name}"
            authors = [
                {{ name = "Keyu Li" }},
                {{ name = "Junhao Shi" }},
                {{ name = "Yang Xiao" }},
                {{ name = "GAIR-NLP AgencyBench Authors" }},
            ]
            keywords = {tags}

            [metadata]
            difficulty = "{spec.difficulty}"
            category = "{spec.category.lower()}"
            tags = {tags}
            upstream_scenario_id = "{spec.scenario_id}"
            upstream_subtask_count = {spec.subtask_count}
            upstream_commit = "{UPSTREAM_COMMIT}"
            runner_family = "{spec.runner_family}"
            early_break_rule = "{spec.early_break_rule}"
            carryover_rule = "{spec.carryover_rule}"

            [agent]
            timeout_sec = 3600.0

            [verifier]
            timeout_sec = 600.0

            [environment]
            build_timeout_sec = 600.0
            cpus = 2
            memory_mb = 4096
            storage_mb = 10240
            """
        )

    @staticmethod
    def _render_instruction_md(spec: ScenarioSpec, description: dict) -> str:
        sub1 = description.get("subtask1", "")
        if isinstance(sub1, dict):
            parts = []
            for key in ("query", "deliverables"):
                if sub1.get(key):
                    parts.append(f"{key.capitalize()}:\n{sub1[key]}")
            sub1_text = "\n\n".join(parts)
        else:
            sub1_text = str(sub1)

        if spec.runner_family == "backend_cpp_rubric":
            notice = (
                "You are working in `/workspace`. Do not access outside paths.\n"
                "Implement the solution in C++17 strictly following the specification below."
            )
        elif spec.runner_family == "code_mse_rubric":
            notice = (
                "You are working in `/workspace`. Do not access outside paths.\n"
                "Inspect the initial workspace assets (`analysis.py`, `equation.parquet`, `equation.py`, "
                "`evaluate_equation.py`) and follow the specification below."
            )
        elif spec.runner_family == "mcp_filesystem":
            notice = (
                "You are operating inside `/workspace`.\n"
                "Do not access files outside `/workspace`. The original source files live in `desktop/`. "
                "The destination hierarchy must be built inside `desktop_2/workspace_v2` (under `desktop_2/`)."
            )
        else:
            notice = "You are working in `/workspace`. Do not access outside paths."

        header = textwrap.dedent(
            f"""\
            <!-- agencybench_task_id: {spec.task_id} -->
            <!-- agencybench_scenario_id: {spec.scenario_id} -->
            <!-- agencybench_stage: subtask1 of {spec.subtask_count} -->

            # AgencyBench V2 — {spec.scenario_id} (Stage 1 of {spec.subtask_count})
            """
        ).strip()
        protocol = textwrap.dedent(
            f"""\
            > **Multi-stage protocol:** This scenario consists of {spec.subtask_count} sequential stages.
            > Only Stage 1 (`subtask1`) is authorized below. Subsequent stages are released sequentially
            > with evaluator feedback by the `AgencyBenchAgent` controller after each stage evaluation.
            """
        ).strip()
        return (
            f"{header}\n\n{notice.strip()}\n\n{protocol}\n\n"
            f"## Stage 1 (`subtask1`)\n\n{sub1_text.strip()}\n"
        )

    @staticmethod
    def _populate_environment(spec: ScenarioSpec, upstream_scenario: Path, env_dir: Path) -> None:
        if spec.runner_family == "backend_cpp_rubric":
            dockerfile = textwrap.dedent(
                """\
                FROM debian:bookworm-slim

                RUN apt-get update && apt-get install -y --no-install-recommends \\
                    g++=4:12.2.0-3 \\
                    make \\
                    libssl-dev \\
                    python3 \\
                    ca-certificates \\
                    && rm -rf /var/lib/apt/lists/*

                WORKDIR /workspace
                """
            )
            (env_dir / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        elif spec.runner_family == "code_mse_rubric":
            source_dir = upstream_scenario / "source"
            assets_dir = env_dir / "source"
            safe_copy_full(source_dir, assets_dir)
            dockerfile = textwrap.dedent(
                """\
                FROM python:3.12-slim

                RUN pip install --no-cache-dir \\
                    numpy==2.1.3 \\
                    scipy==1.14.1 \\
                    pandas==2.2.3 \\
                    pyarrow==18.1.0

                WORKDIR /workspace
                COPY source/ /workspace/
                """
            )
            (env_dir / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        elif spec.runner_family == "backend_java_rubric":
            dockerfile = textwrap.dedent(
                """\
                FROM debian:bookworm-slim

                RUN apt-get update && apt-get install -y --no-install-recommends \\
                    openjdk-17-jdk-headless \\
                    python3 \\
                    ca-certificates \\
                    && rm -rf /var/lib/apt/lists/*

                WORKDIR /workspace
                """
            )
            (env_dir / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        elif spec.runner_family in ("backend_py_rubric", "research_rubric"):
            dockerfile = textwrap.dedent(
                """\
                FROM python:3.12-slim

                WORKDIR /workspace
                """
            )
            (env_dir / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        elif spec.runner_family == "code_loss_single":
            ws_dir = upstream_scenario / "workspace"
            assets_dir = env_dir / "workspace"
            safe_copy_full(ws_dir, assets_dir)
            dockerfile = textwrap.dedent(
                """\
                FROM python:3.12-slim

                RUN pip install --no-cache-dir \\
                    numpy==2.1.3 \\
                    scipy==1.14.1 \\
                    pandas==2.2.3 \\
                    pyarrow==18.1.0

                WORKDIR /workspace
                COPY workspace/ /workspace/
                """
            )
            (env_dir / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        elif spec.runner_family == "mcp_filesystem":
            desktop_dir = upstream_scenario / "desktop"
            assets_dir = env_dir / "desktop"
            safe_copy_full(desktop_dir, assets_dir)
            dockerfile = textwrap.dedent(
                """\
                FROM python:3.12-slim

                WORKDIR /workspace
                COPY desktop/ /workspace/desktop/
                RUN mkdir -p /workspace/desktop_2
                """
            )
            (env_dir / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        else:
            raise NotImplementedError(f"Unsupported Dockerfile family: {spec.runner_family}")

    @staticmethod
    def _render_test_sh(spec: ScenarioSpec) -> str:
        return textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -euo pipefail

            mkdir -p /logs/verifier

            python3 /tests/run_verifier.py \\
              --runner-family "{spec.runner_family}" \\
              --eval-script /tests/upstream/eval_task.py \\
              --description /tests/upstream/description.json \\
              --workspace /workspace \\
              --stages-dir /tmp/.agencybench_stages \\
              --reward-txt /logs/verifier/reward.txt \\
              --reward-json /logs/verifier/reward.json \\
              --report-json /logs/verifier/agencybench_report.json
            """
        )
