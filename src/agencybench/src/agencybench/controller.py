"""Trusted multi-stage scenario controller for AgencyBench V2.

Preserves exact per-scenario upstream execution semantics:
- Stage-by-stage prompt release (subtask_k only; future subtasks remain hidden).
- Exact per-scenario attempt limit, early-break cutoff, feedback text, copy
  exclusion filter, and next-stage workspace carry-over / tie-breaking.
- Pluggable AgentTransport so the same controller drives Harbor container agents,
  native SII SDK agents, standard CLI agents (Claude Code / Codex), or
  deterministic test/oracle fixtures.
"""

from __future__ import annotations

import json
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .registry import ScenarioSpec
from .sandbox_io import (
    clear_directory,
    hash_directory,
    safe_copy_backend_filtered,
    safe_copy_full,
)
from .verifier import load_upstream_module


class AgentTransport(Protocol):
    """Minimal transport seam replacing upstream `AgentRunner` / `SiiAgentSession`."""

    def send(
        self,
        *,
        subtask: str,
        attempt: int,
        prompt: str,
        workspace_notice: str,
        workspace: Path,
    ) -> str:
        """Execute one agent turn inside `workspace` and return assistant text."""
        ...


@dataclass
class AttemptRecord:
    subtask: str
    attempt_index: int
    score: float
    passed: bool
    workspace: Path
    evalspace: Path
    workspace_sha256: str
    prompt_sent: str
    feedback: str
    agent_output: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "subtask": self.subtask,
            "attempt_index": self.attempt_index,
            "score": self.score,
            "passed": self.passed,
            "workspace": str(self.workspace),
            "evalspace": str(self.evalspace),
            "workspace_sha256": self.workspace_sha256,
            "feedback": self.feedback,
            "agent_output": self.agent_output[:2000],
            "details": self.details,
        }


@dataclass
class SubtaskRecord:
    subtask: str
    best_score: float
    best_attempt: int | None
    selected_workspace: Path | None
    attempts: list[AttemptRecord]
    skipped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.subtask,
            "best_score": self.best_score,
            "best_attempt": self.best_attempt,
            "selected_workspace": str(self.selected_workspace) if self.selected_workspace else None,
            "skipped": self.skipped,
            "attempts": [a.to_dict() for a in self.attempts],
        }


@dataclass
class ScenarioRunResult:
    scenario_id: str
    task_id: str
    runner_family: str
    subtasks: list[SubtaskRecord]
    reward: float
    s_avg_pct: float
    pass_at_1: float
    pass_at_2: float
    avg_attempts: float
    stages_dir: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "task_id": self.task_id,
            "runner_family": self.runner_family,
            "reward": self.reward,
            "s_avg_pct": self.s_avg_pct,
            "pass_at_1": self.pass_at_1,
            "pass_at_2": self.pass_at_2,
            "avg_attempts": self.avg_attempts,
            "stages_dir": str(self.stages_dir),
            "subtasks": [s.to_dict() for s in self.subtasks],
        }


class ScenarioController:
    """Trusted controller implementing exact upstream AgencyBench V2 protocols."""

    def __init__(
        self,
        *,
        spec: ScenarioSpec,
        upstream_scenario_dir: Path,
        run_root: Path,
        max_attempts: int | None = None,
    ) -> None:
        self.spec = spec
        self.upstream_scenario_dir = upstream_scenario_dir.resolve()
        self.run_root = run_root.resolve()
        self.max_attempts = max_attempts or spec.default_max_attempts
        self.description = json.loads(
            (self.upstream_scenario_dir / "description.json").read_text(encoding="utf-8")
        )
        self.eval_mod = load_upstream_module(
            self.upstream_scenario_dir / "eval_task.py",
            module_name=f"agencybench_ctrl_{spec.task_id.replace('-', '_')}",
        )
        self.stages_dir = self.run_root / "stages"

    def run(self, transport: AgentTransport) -> ScenarioRunResult:
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.stages_dir.mkdir(parents=True, exist_ok=True)

        if self.spec.runner_family in ("backend_cpp_rubric", "backend_java_rubric"):
            subtasks = self._run_backend_cpp(transport)
        elif self.spec.runner_family == "backend_py_rubric":
            subtasks = self._run_backend_py(transport)
        elif self.spec.runner_family == "code_mse_rubric":
            subtasks = self._run_code_mse(transport)
        elif self.spec.runner_family == "code_loss_single":
            subtasks = self._run_code_loss_single(transport)
        elif self.spec.runner_family == "mcp_filesystem":
            subtasks = self._run_mcp_filesystem(transport)
        elif self.spec.runner_family == "research_rubric":
            subtasks = self._run_research_rubric(transport)
        else:
            raise ValueError(f"Controller does not yet support {self.spec.runner_family}")

        return self._summarize(subtasks)

    def _run_backend_cpp(self, transport: AgentTransport) -> list[SubtaskRecord]:
        """Exact Backend/scenario1 EvaluationCoordinator semantics."""
        rubric_eval = self.eval_mod.RubricEvaluator()
        subtask_count = int(self.description.get("subtask_count") or 0)
        previous_best: Path | None = None
        records: list[SubtaskRecord] = []

        for idx in range(1, subtask_count + 1):
            subtask = f"subtask{idx}"
            base_prompt = str(self.description.get(subtask, ""))
            attempt_records: list[AttemptRecord] = []
            cache_workspace: Path | None = None
            feedback = ""

            for attempt in range(1, self.max_attempts + 1):
                workspace = self.run_root / subtask / "workspace" / f"attempt_{attempt:02d}"
                evalspace = self.run_root / subtask / "evalspace" / f"attempt_{attempt:02d}"
                clear_directory(workspace)
                clear_directory(evalspace)

                if attempt > 1:
                    prev_ws = self.run_root / subtask / "workspace" / f"attempt_{attempt - 1:02d}"
                    if prev_ws.exists():
                        safe_copy_full(prev_ws, workspace)
                elif previous_best is not None and previous_best.exists():
                    safe_copy_full(previous_best, workspace)

                notice = textwrap.dedent(
                    f"""
                    You are working in {workspace.name}. Do not access outside paths.
                    Implement the solution in C++17 strictly following the JSON specifications.
                    """
                ).strip()
                prompt_sent = base_prompt + ("\n\n" + feedback if feedback else "")
                agent_output = transport.send(
                    subtask=subtask,
                    attempt=attempt,
                    prompt=prompt_sent,
                    workspace_notice=notice,
                    workspace=workspace,
                )

                # Exact Backend/scenario1 filtered copy to isolated evalspace.
                safe_copy_backend_filtered(workspace, evalspace)
                logs_dir = evalspace / "logs"
                logs_dir.mkdir(parents=True, exist_ok=True)
                cmd_runner = self.eval_mod.CommandRunner(logs_dir, conda_env=None, conda_exe=None)
                commands = cmd_runner.capture(subtask, evalspace)
                rubric_res = rubric_eval.evaluate(subtask, evalspace, commands)
                feedback = self._backend_feedback(rubric_res)

                attempt_records.append(
                    AttemptRecord(
                        subtask=subtask,
                        attempt_index=attempt,
                        score=float(rubric_res.score),
                        passed=int(rubric_res.score) == 10,
                        workspace=workspace,
                        evalspace=evalspace,
                        workspace_sha256=hash_directory(workspace),
                        prompt_sent=prompt_sent,
                        feedback=feedback,
                        agent_output=agent_output,
                        details={
                            "pass_count": rubric_res.pass_count,
                            "total_points": rubric_res.total_points,
                            "failed_points": list(rubric_res.failed_points),
                        },
                    )
                )

                # Exact Backend/scenario1 L1081-1084:
                # Early break ONLY on score == 10; otherwise retain first attempt workspace!
                if int(rubric_res.score) == 10:
                    cache_workspace = workspace
                    break
                cache_workspace = cache_workspace or workspace

            best = max(attempt_records, key=lambda a: a.score) if attempt_records else None
            if cache_workspace is None and best is not None:
                cache_workspace = best.workspace
            if cache_workspace is not None:
                previous_best = cache_workspace

            # Stage snapshot stores the best-scoring attempt for independent verifier replay.
            stage_dst = self.stages_dir / subtask
            if best is not None:
                safe_copy_full(best.workspace, stage_dst)

            records.append(
                SubtaskRecord(
                    subtask=subtask,
                    best_score=best.score if best else 0.0,
                    best_attempt=best.attempt_index if best else None,
                    selected_workspace=previous_best,
                    attempts=attempt_records,
                )
            )

        return records

    @staticmethod
    def _backend_feedback(rubric_res: Any) -> str:
        if not rubric_res.failed_points:
            return f"All {rubric_res.total_points} rubric points passed (score={rubric_res.score}/10)."
        bullets = "\n".join(f"- {pt}" for pt in rubric_res.failed_points)
        return (
            f"Previous attempt scored {rubric_res.score}/10 "
            f"({rubric_res.pass_count}/{rubric_res.total_points} passed).\n"
            f"Failed rubric points to fix:\n{bullets}"
        )

    def _run_code_mse(self, transport: AgentTransport) -> list[SubtaskRecord]:
        """Exact Code/scenario1 EvaluationCoordinator semantics."""
        rubric_eval = self.eval_mod.RubricEvaluator()
        subtask_count = int(self.description.get("subtask_count") or 0)
        source_dir = self.upstream_scenario_dir / "source"
        previous_best_workspace: Path | None = None
        records: list[SubtaskRecord] = []

        for idx in range(1, subtask_count + 1):
            subtask = f"subtask{idx}"
            base_prompt = str(self.description.get(subtask, ""))
            attempt_records: list[AttemptRecord] = []
            feedback = ""

            for attempt in range(1, self.max_attempts + 1):
                workspace = self.run_root / subtask / "workspace" / f"attempt_{attempt:02d}"
                evalspace = self.run_root / subtask / "evalspace" / f"attempt_{attempt:02d}"
                clear_directory(workspace)
                clear_directory(evalspace)

                if attempt == 1:
                    if idx == 1 and source_dir.is_dir():
                        safe_copy_full(source_dir, workspace)
                    elif previous_best_workspace is not None and previous_best_workspace.exists():
                        safe_copy_full(previous_best_workspace, workspace)
                else:
                    prev_ws = self.run_root / subtask / "workspace" / f"attempt_{attempt - 1:02d}"
                    if prev_ws.exists():
                        safe_copy_full(prev_ws, workspace)

                notice = textwrap.dedent(
                    f"""
                    You are working in {workspace.name}. Do not access outside paths.
                    Modify equation.py and generate required logs/docs strictly following the specification.
                    """
                ).strip()
                prompt_sent = base_prompt + ("\n\n" + feedback if feedback else "")
                agent_output = transport.send(
                    subtask=subtask,
                    attempt=attempt,
                    prompt=prompt_sent,
                    workspace_notice=notice,
                    workspace=workspace,
                )

                safe_copy_full(workspace, evalspace)
                logs_dir = evalspace / "logs"
                logs_dir.mkdir(parents=True, exist_ok=True)
                cmd_runner = self.eval_mod.CommandRunner(logs_dir, conda_env=None, conda_exe=None)
                commands = cmd_runner.capture(subtask, evalspace)
                rubric_res = rubric_eval.evaluate(subtask, evalspace, commands)
                feedback = self._code_mse_feedback(rubric_res)

                attempt_records.append(
                    AttemptRecord(
                        subtask=subtask,
                        attempt_index=attempt,
                        score=float(rubric_res.score),
                        passed=bool(rubric_res.passed),
                        workspace=workspace,
                        evalspace=evalspace,
                        workspace_sha256=hash_directory(workspace),
                        prompt_sent=prompt_sent,
                        feedback=feedback,
                        agent_output=agent_output,
                        details={"loss": rubric_res.loss, "notes": list(rubric_res.notes)},
                    )
                )

                # Exact Code/scenario1 L898: break at score >= 9.9.
                if float(rubric_res.score) >= 9.9:
                    break

            # Exact Code/scenario1 L905-909: highest-scoring attempt (first on tie).
            best = max(attempt_records, key=lambda a: a.score) if attempt_records else None
            if best is not None:
                previous_best_workspace = best.workspace
                safe_copy_full(best.workspace, self.stages_dir / subtask)

            records.append(
                SubtaskRecord(
                    subtask=subtask,
                    best_score=best.score if best else 0.0,
                    best_attempt=best.attempt_index if best else None,
                    selected_workspace=previous_best_workspace,
                    attempts=attempt_records,
                )
            )

        return records

    @staticmethod
    def _code_mse_feedback(rubric_res: Any) -> str:
        parts = []
        if rubric_res.loss is not None:
            parts.append(f"Current MSE: {rubric_res.loss}")
        else:
            parts.append("Could not determine MSE from output.")
        if rubric_res.notes:
            parts.append("Issues: " + "; ".join(rubric_res.notes))
        parts.append(f"Score: {rubric_res.score:.2f}/10.")
        return "\n".join(parts)

    def _run_mcp_filesystem(self, transport: AgentTransport) -> list[SubtaskRecord]:
        """Exact MCP/scenario2 Task26Evaluator semantics (in-place + halt-on-fail)."""
        validators = self.eval_mod.SUBTASK_VALIDATORS
        hints = getattr(self.eval_mod, "SUBTASK_ACCEPTANCE_HINTS", {})
        subtask_count = int(self.description.get("subtask_count") or 0)

        workspace = self.run_root / "workspace"
        clear_directory(workspace)
        template_desktop = self.upstream_scenario_dir / "desktop"
        safe_copy_full(template_desktop, workspace / "desktop")
        (workspace / "desktop_2").mkdir(parents=True, exist_ok=True)

        records: list[SubtaskRecord] = []
        skip_remaining = False

        for idx in range(1, subtask_count + 1):
            subtask = f"subtask{idx}"
            instructions = str(self.description.get(subtask, ""))
            if skip_remaining:
                records.append(
                    SubtaskRecord(
                        subtask=subtask,
                        best_score=0.0,
                        best_attempt=None,
                        selected_workspace=None,
                        attempts=[],
                        skipped=True,
                    )
                )
                continue

            validator = validators[subtask]
            attempt_records: list[AttemptRecord] = []
            previous_feedback = ""
            succeeded = False

            for attempt in range(1, self.max_attempts + 1):
                notice = (
                    "You are operating inside the dedicated task26 workspace. "
                    "The original source files live in 'desktop/'. "
                    "The destination hierarchy must be built inside 'desktop_2/workspace_v2'."
                )
                lines = [
                    notice,
                    f"Current focus: {subtask} (attempt {attempt}/{self.max_attempts}).",
                    "Follow the exact requirements from description.json for this step.",
                    instructions.strip(),
                ]
                if subtask in hints:
                    lines.append(f"Evaluator check: {hints[subtask]}")
                if previous_feedback:
                    lines.append(f"Previous attempt feedback: {previous_feedback}")
                prompt_sent = "\n\n".join(lines)

                agent_output = transport.send(
                    subtask=subtask,
                    attempt=attempt,
                    prompt=prompt_sent,
                    workspace_notice=notice,
                    workspace=workspace,
                )

                evalspace = self.run_root / "evalspace" / f"{subtask}_attempt_{attempt:02d}"
                safe_copy_full(workspace, evalspace)
                ok, message = validator(evalspace)
                score = 10.0 if ok else 0.0
                attempt_records.append(
                    AttemptRecord(
                        subtask=subtask,
                        attempt_index=attempt,
                        score=score,
                        passed=bool(ok),
                        workspace=workspace,
                        evalspace=evalspace,
                        workspace_sha256=hash_directory(workspace),
                        prompt_sent=prompt_sent,
                        feedback=message,
                        agent_output=agent_output,
                        details={"validator_message": message},
                    )
                )
                if ok:
                    succeeded = True
                    break
                previous_feedback = message

            # Snapshot workspace state at the end of this stage for independent verifier replay.
            safe_copy_full(workspace, self.stages_dir / subtask)
            best = max(attempt_records, key=lambda a: a.score) if attempt_records else None
            records.append(
                SubtaskRecord(
                    subtask=subtask,
                    best_score=10.0 if succeeded else 0.0,
                    best_attempt=best.attempt_index if best else None,
                    selected_workspace=workspace,
                    attempts=attempt_records,
                )
            )
            if not succeeded:
                skip_remaining = True

        return records


    def _run_backend_py(self, transport: AgentTransport) -> list[SubtaskRecord]:
        """Exact Backend/scenario3 EvaluationCoordinator semantics (including L1348 score >= 0 break)."""
        rubric_eval = self.eval_mod.RubricEvaluator()
        subtask_count = int(self.description.get("subtask_count") or 0)
        previous_best: Path | None = None
        records: list[SubtaskRecord] = []

        for idx in range(1, subtask_count + 1):
            subtask = f"subtask{idx}"
            base_prompt = str(self.description.get(subtask, ""))
            attempt_records: list[AttemptRecord] = []
            cache_workspace: Path | None = None
            feedback = ""

            for attempt in range(1, self.max_attempts + 1):
                workspace = self.run_root / subtask / "workspace" / f"attempt_{attempt:02d}"
                evalspace = self.run_root / subtask / "evalspace" / f"attempt_{attempt:02d}"
                clear_directory(workspace)
                clear_directory(evalspace)

                if attempt > 1:
                    prev_ws = self.run_root / subtask / "workspace" / f"attempt_{attempt - 1:02d}"
                    if prev_ws.exists():
                        safe_copy_full(prev_ws, workspace)
                elif previous_best is not None and previous_best.exists():
                    safe_copy_full(previous_best, workspace)

                notice = f"You are working in {workspace.name}. Do not access outside paths."
                prompt_sent = base_prompt + ("\n\n" + feedback if feedback else "")
                agent_output = transport.send(
                    subtask=subtask,
                    attempt=attempt,
                    prompt=prompt_sent,
                    workspace_notice=notice,
                    workspace=workspace,
                )

                safe_copy_full(workspace, evalspace)
                logs_dir = evalspace / "logs"
                logs_dir.mkdir(parents=True, exist_ok=True)
                runner = self.eval_mod.TestPlanRunner(evalspace, logs_dir, conda_env=None, conda_exe=None)
                rubric_res = rubric_eval.evaluate(subtask, evalspace, runner)
                notes = list(getattr(rubric_res, "notes", []) or [])
                feedback = "请根据以下Rubric反馈修正代码：\n" + "\n".join(f"- {n}" for n in notes) if notes else ""

                attempt_records.append(
                    AttemptRecord(
                        subtask=subtask,
                        attempt_index=attempt,
                        score=float(rubric_res.score),
                        passed=float(rubric_res.score) >= 6.0,
                        workspace=workspace,
                        evalspace=evalspace,
                        workspace_sha256=hash_directory(workspace),
                        prompt_sent=prompt_sent,
                        feedback=feedback,
                        agent_output=agent_output,
                        details={"metrics": dict(rubric_res.metrics), "notes": notes},
                    )
                )
                # Upstream Backend/scenario3 eval_task.py L1348 breaks whenever score >= 0
                if rubric_res.score >= 0:
                    cache_workspace = workspace
                    break
                cache_workspace = cache_workspace or workspace

            best = max(attempt_records, key=lambda a: a.score) if attempt_records else None
            if cache_workspace is None and best:
                cache_workspace = best.workspace
            if cache_workspace is not None:
                previous_best = cache_workspace
                safe_copy_full(cache_workspace, self.stages_dir / subtask)

            records.append(
                SubtaskRecord(
                    subtask=subtask,
                    best_score=best.score if best else 0.0,
                    best_attempt=best.attempt_index if best else None,
                    selected_workspace=cache_workspace,
                    attempts=attempt_records,
                )
            )

        return records

    def _run_code_loss_single(self, transport: AgentTransport) -> list[SubtaskRecord]:
        """Exact Code/scenario6 & Code/scenario7 single-subtask equation discovery protocol."""
        import tempfile
        from .verifier import evaluate_code_loss_single

        subtask = "subtask1"
        base_prompt = str(self.description.get(subtask, ""))
        workspace = self.run_root / "workspace"
        clear_directory(workspace)
        safe_copy_full(self.upstream_scenario_dir / "workspace", workspace)

        attempt_records: list[AttemptRecord] = []
        previous_feedback = ""
        threshold = 1e-7

        for attempt in range(1, self.max_attempts + 1):
            res_file = workspace / "subtask1_result.json"
            if res_file.exists():
                res_file.unlink()

            notice = f"You must work only inside {workspace.name}."
            prompt_sent = base_prompt
            if previous_feedback:
                prompt_sent += f"\n\nPrevious attempt feedback: {previous_feedback}"

            agent_output = transport.send(
                subtask=subtask,
                attempt=attempt,
                prompt=prompt_sent,
                workspace_notice=notice,
                workspace=workspace,
            )

            evalspace = self.run_root / "evalspace" / f"attempt_{attempt:02d}"
            safe_copy_full(workspace, evalspace)
            with tempfile.TemporaryDirectory(prefix="agencybench_ctrl_loss_") as tmp:
                ver_res = evaluate_code_loss_single(
                    self.eval_mod,
                    self.description,
                    workspace,
                    None,
                    Path(tmp),
                    upstream_eval_script=self.upstream_scenario_dir / "eval_task.py",
                )
            st_info = ver_res["subtasks"][0]
            score = float(st_info["score"])
            eff_loss = st_info.get("effective_loss")
            passed = eff_loss is not None and eff_loss <= threshold
            feedback = "; ".join(st_info.get("notes") or []) or (
                f"Loss {eff_loss} > target {threshold}" if not passed else "Met threshold"
            )

            attempt_records.append(
                AttemptRecord(
                    subtask=subtask,
                    attempt_index=attempt,
                    score=score,
                    passed=passed,
                    workspace=workspace,
                    evalspace=evalspace,
                    workspace_sha256=hash_directory(workspace),
                    prompt_sent=prompt_sent,
                    feedback=feedback,
                    agent_output=agent_output,
                    details=st_info,
                )
            )
            if passed:
                break
            previous_feedback = feedback

        safe_copy_full(workspace, self.stages_dir / subtask)
        best = max(attempt_records, key=lambda a: a.score) if attempt_records else None
        return [
            SubtaskRecord(
                subtask=subtask,
                best_score=best.score if best else 0.0,
                best_attempt=best.attempt_index if best else None,
                selected_workspace=workspace,
                attempts=attempt_records,
            )
        ]

    def _run_research_rubric(self, transport: AgentTransport) -> list[SubtaskRecord]:
        """Exact Research/scenario2 & Research/scenario3 in-place RubricEvaluator protocol."""
        rubric_eval = self.eval_mod.RubricEvaluator()
        subtask_count = int(self.description.get("subtask_count") or 0)
        workspace = self.run_root / "workspace"
        clear_directory(workspace)
        records: list[SubtaskRecord] = []

        target_files_s2 = {
            "subtask1": "subtask1.md",
            "subtask2": "subtask2.md",
            "subtask3": "subtask3.md",
            "subtask4": "subtask4.md",
            "subtask5": "subtask_summary.json",
        }

        for idx in range(1, subtask_count + 1):
            subtask = f"subtask{idx}"
            entry = self.description.get(subtask, "")
            if isinstance(entry, dict):
                q = str(entry.get("query", "")).strip()
                d = str(entry.get("deliverables", "")).strip()
                base_prompt = f"{q}\n\nDeliverables:\n{d}" if (q and d) else "\n\n".join(p for p in (q, d) if p)
            else:
                base_prompt = str(entry)

            attempt_records: list[AttemptRecord] = []
            feedback = ""

            for attempt in range(1, self.max_attempts + 1):
                if self.spec.scenario_id == "Research/scenario2":
                    tf = target_files_s2.get(subtask)
                    if tf and (workspace / tf).exists():
                        (workspace / tf).unlink()

                notice = f"You are working in {workspace.name}. Do not access outside paths."
                prompt_sent = f"{base_prompt}\n\n{feedback}" if feedback else base_prompt
                agent_output = transport.send(
                    subtask=subtask,
                    attempt=attempt,
                    prompt=prompt_sent,
                    workspace_notice=notice,
                    workspace=workspace,
                )

                evalspace = self.run_root / "evalspace" / f"{subtask}_attempt_{attempt:02d}"
                safe_copy_full(workspace, evalspace)
                rubric_res = rubric_eval.evaluate(subtask, evalspace)
                notes = list(getattr(rubric_res, "notes", []) or [])
                feedback = "Please address these rubric gaps:\n" + "\n".join(f"- {n}" for n in notes) if notes else ""

                attempt_records.append(
                    AttemptRecord(
                        subtask=subtask,
                        attempt_index=attempt,
                        score=float(rubric_res.score),
                        passed=float(rubric_res.score) >= 9.99,
                        workspace=workspace,
                        evalspace=evalspace,
                        workspace_sha256=hash_directory(workspace),
                        prompt_sent=prompt_sent,
                        feedback=feedback,
                        agent_output=agent_output,
                        details={"notes": notes},
                    )
                )
                if float(rubric_res.score) >= 9.99:
                    break

            safe_copy_full(workspace, self.stages_dir / subtask)
            best = max(attempt_records, key=lambda a: a.score) if attempt_records else None
            records.append(
                SubtaskRecord(
                    subtask=subtask,
                    best_score=best.score if best else 0.0,
                    best_attempt=best.attempt_index if best else None,
                    selected_workspace=workspace,
                    attempts=attempt_records,
                )
            )

        return records

    def _summarize(self, subtasks: list[SubtaskRecord]) -> ScenarioRunResult:
        n = len(subtasks)
        mean_raw = sum(s.best_score for s in subtasks) / n if n else 0.0
        reward = round(max(0.0, min(1.0, mean_raw / 10.0)), 6)
        s_avg_pct = round(reward * 100.0, 4)

        pass1 = 0
        pass2 = 0
        total_attempts = 0
        for s in subtasks:
            total_attempts += len(s.attempts)
            if s.attempts and s.attempts[0].score >= 6.0:
                pass1 += 1
            if any(a.score >= 6.0 for a in s.attempts[:2]):
                pass2 += 1

        result = ScenarioRunResult(
            scenario_id=self.spec.scenario_id,
            task_id=self.spec.task_id,
            runner_family=self.spec.runner_family,
            subtasks=subtasks,
            reward=reward,
            s_avg_pct=s_avg_pct,
            pass_at_1=round(pass1 / n, 6) if n else 0.0,
            pass_at_2=round(pass2 / n, 6) if n else 0.0,
            avg_attempts=round(total_attempts / n, 6) if n else 0.0,
            stages_dir=self.stages_dir,
        )
        (self.run_root / "controller_ledger.json").write_text(
            json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8"
        )
        return result
