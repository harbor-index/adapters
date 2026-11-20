#!/usr/bin/env python3
"""Independent Harbor verifier for AgencyBench V2 tasks.

This module is both importable by adapter tests/controller and copied directly
into each generated task's `tests/run_verifier.py` (which is mounted read-only
at `/tests/` ONLY during Harbor's verifier phase, after the agent exits).

Key invariants:
- NEVER trusts any score, reward, or `meta_eval.json` written by the evaluated agent.
- ALWAYS re-executes the unaltered pinned upstream `eval_task.py` evaluator
  against isolated evalspace copies of the deliverables.
- Enforces allow-listed workspace copying and rejects symlink escapes.
- Emits normalized numeric reward in [0.0, 1.0] to `/logs/verifier/reward.txt`,
  strictly flat numeric metrics to `/logs/verifier/reward.json` (Harbor schema),
  and full structured diagnostics to `/logs/verifier/agencybench_report.json`.
- Fails closed (non-zero exit, explicit error classification) on verifier errors.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import stat
import sys
import tempfile
import traceback
from pathlib import Path
from types import ModuleType
from typing import Any

# Exact Backend/scenario1 exclusions (mirrored here so run_verifier.py is standalone).
BACKEND_EXCLUDED_COPY_DIRS = frozenset(
    {".git", ".hg", ".svn", "__pycache__", ".sii", "logs", "bin"}
)
BACKEND_EXCLUDED_COPY_FILES = frozenset({"meta_eval.json"})
BACKEND_EXCLUDED_COPY_SUFFIXES = frozenset({".db", ".db3", ".sqlite", ".sqlite3", ".txt"})


class VerifierSecurityError(RuntimeError):
    """Raised when workspace files contain symlink escapes or unsafe special files."""


def _safe_copy_tree(
    src: Path,
    dst: Path,
    *,
    skip_hidden: bool = True,
    excluded_dirs: frozenset[str] = frozenset(),
    excluded_files: frozenset[str] = frozenset(),
    excluded_suffixes: frozenset[str] = frozenset(),
) -> None:
    src_root = src.resolve()
    dst_root = dst.resolve()
    dst_root.mkdir(parents=True, exist_ok=True)

    for root, dirs, files in os.walk(src_root, followlinks=False):
        root_path = Path(root)
        rel_root = root_path.relative_to(src_root)
        if any(
            (skip_hidden and part.startswith(".")) or part in excluded_dirs
            for part in rel_root.parts
        ):
            dirs[:] = []
            continue

        kept: list[str] = []
        for dname in sorted(dirs):
            if (skip_hidden and dname.startswith(".")) or dname in excluded_dirs:
                continue
            dpath = root_path / dname
            st = os.lstat(dpath)
            if stat.S_ISLNK(st.st_mode):
                raise VerifierSecurityError(f"Refusing directory symlink: {dpath}")
            kept.append(dname)
        dirs[:] = kept

        target_root = dst_root / rel_root
        target_root.mkdir(parents=True, exist_ok=True)
        for fname in sorted(files):
            if skip_hidden and fname.startswith("."):
                continue
            if fname in excluded_files:
                continue
            if Path(fname).suffix.lower() in excluded_suffixes:
                continue
            fpath = root_path / fname
            st = os.lstat(fpath)
            if stat.S_ISLNK(st.st_mode):
                resolved = fpath.resolve()
                try:
                    resolved.relative_to(src_root)
                except ValueError as exc:
                    raise VerifierSecurityError(
                        f"Refusing escaping symlink: {fpath} -> {resolved}"
                    ) from exc
            elif not stat.S_ISREG(st.st_mode):
                raise VerifierSecurityError(f"Refusing non-regular file: {fpath}")
            shutil.copy2(fpath, target_root / fname)


def _ensure_optional_sii_stubs() -> list[str]:
    """Install lightweight placeholders for sii_agent_sdk ONLY if absent.

    Some unaltered upstream eval_task.py files (e.g., MCP/scenario2) import
    sii_agent_sdk at module top level even though their validators are pure stdlib.
    """
    if "sii_agent_sdk" in sys.modules:
        return []
    try:
        __import__("sii_agent_sdk")
        return []
    except ImportError:
        pass

    class _StubAttr:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

    class _StubMod(ModuleType):
        def __getattr__(self, name: str) -> Any:
            return _StubAttr

    submods = [
        "sii_agent_sdk",
        "sii_agent_sdk._internal",
        "sii_agent_sdk._internal.event_logger",
        "sii_agent_sdk._internal.message_parser",
        "sii_agent_sdk.bridge",
        "sii_agent_sdk.errors",
        "sii_agent_sdk.query",
        "sii_agent_sdk.session_state",
    ]
    installed: list[str] = []
    for name in submods:
        if name not in sys.modules:
            m = _StubMod(name)
            m.__path__ = []  # type: ignore[attr-defined]
            sys.modules[name] = m
            installed.append(name)
    return installed


def load_upstream_module(eval_task_path: Path, module_name: str = "agencybench_upstream_eval") -> ModuleType:
    """Dynamically load a pinned upstream `eval_task.py` module without mutating it."""
    eval_task_path = eval_task_path.resolve()
    if not eval_task_path.is_file():
        raise FileNotFoundError(f"Upstream evaluator missing: {eval_task_path}")
    _ensure_optional_sii_stubs()
    spec = importlib.util.spec_from_file_location(module_name, eval_task_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module spec from {eval_task_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _resolve_stage_source(
    subtask: str,
    workspace: Path,
    stages_dir: Path | None,
) -> tuple[Path, str]:
    """Return (source_dir, source_kind) for a subtask."""
    if stages_dir is not None:
        candidate = stages_dir / subtask
        if candidate.is_dir():
            return candidate, "stage_snapshot"
    return workspace, "workspace_direct"


def evaluate_backend_cpp_rubric(
    eval_mod: ModuleType,
    description: dict[str, Any],
    workspace: Path,
    stages_dir: Path | None,
    work_tmp: Path,
) -> dict[str, Any]:
    """Replay Backend/scenario1 unaltered CommandRunner + RubricEvaluator."""
    subtask_count = int(description.get("subtask_count") or 0)
    rubric_eval = eval_mod.RubricEvaluator()
    subtask_records: list[dict[str, Any]] = []

    for idx in range(1, subtask_count + 1):
        subtask = f"subtask{idx}"
        src_dir, src_kind = _resolve_stage_source(subtask, workspace, stages_dir)
        evalspace = work_tmp / f"evalspace_{subtask}"
        _safe_copy_tree(
            src_dir,
            evalspace,
            skip_hidden=True,
            excluded_dirs=BACKEND_EXCLUDED_COPY_DIRS,
            excluded_files=BACKEND_EXCLUDED_COPY_FILES,
            excluded_suffixes=BACKEND_EXCLUDED_COPY_SUFFIXES,
        )
        logs_dir = evalspace / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        cmd_runner = eval_mod.CommandRunner(logs_dir, conda_env=None, conda_exe=None)
        commands = cmd_runner.capture(subtask, evalspace)
        rubric_res = rubric_eval.evaluate(subtask, evalspace, commands)
        subtask_records.append(
            {
                "subtask": subtask,
                "source_kind": src_kind,
                "score": float(rubric_res.score),
                "max_score": 10.0,
                "pass_count": rubric_res.pass_count,
                "total_points": rubric_res.total_points,
                "failed_points": list(rubric_res.failed_points),
                "passed_60pct": float(rubric_res.score) >= 6.0,
                "compile_returncode": commands.get("compile", {}).get("returncode"),
                "probe_returncode": commands.get("probe", {}).get("returncode"),
            }
        )

    return _aggregate_subtask_scores(subtask_records, scale_max=10.0)


def evaluate_code_mse_rubric(
    eval_mod: ModuleType,
    description: dict[str, Any],
    workspace: Path,
    stages_dir: Path | None,
    work_tmp: Path,
) -> dict[str, Any]:
    """Replay Code/scenario1 unaltered CommandRunner + RubricEvaluator."""
    subtask_count = int(description.get("subtask_count") or 0)
    rubric_eval = eval_mod.RubricEvaluator()
    subtask_records: list[dict[str, Any]] = []

    for idx in range(1, subtask_count + 1):
        subtask = f"subtask{idx}"
        src_dir, src_kind = _resolve_stage_source(subtask, workspace, stages_dir)
        evalspace = work_tmp / f"evalspace_{subtask}"
        _safe_copy_tree(src_dir, evalspace, skip_hidden=True)
        logs_dir = evalspace / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        cmd_runner = eval_mod.CommandRunner(logs_dir, conda_env=None, conda_exe=None)
        commands = cmd_runner.capture(subtask, evalspace)
        rubric_res = rubric_eval.evaluate(subtask, evalspace, commands)
        subtask_records.append(
            {
                "subtask": subtask,
                "source_kind": src_kind,
                "score": float(rubric_res.score),
                "max_score": 10.0,
                "passed_upstream": bool(rubric_res.passed),
                "loss": rubric_res.loss,
                "notes": list(rubric_res.notes),
                "passed_60pct": float(rubric_res.score) >= 6.0,
                "eval_returncode": commands.get("evaluate", {}).get("returncode"),
            }
        )

    return _aggregate_subtask_scores(subtask_records, scale_max=10.0)


def evaluate_mcp_filesystem(
    eval_mod: ModuleType,
    description: dict[str, Any],
    workspace: Path,
    stages_dir: Path | None,
    work_tmp: Path,
) -> dict[str, Any]:
    """Replay MCP/scenario2 unaltered SUBTASK_VALIDATORS with halt-on-failure semantics."""
    subtask_count = int(description.get("subtask_count") or 0)
    validators = eval_mod.SUBTASK_VALIDATORS
    subtask_records: list[dict[str, Any]] = []
    halted = False

    for idx in range(1, subtask_count + 1):
        subtask = f"subtask{idx}"
        if halted:
            subtask_records.append(
                {
                    "subtask": subtask,
                    "source_kind": "skipped_after_halt",
                    "score": 0.0,
                    "max_score": 10.0,
                    "success": False,
                    "message": "Skipped because a previous subtask failed.",
                    "passed_60pct": False,
                }
            )
            continue

        src_dir, src_kind = _resolve_stage_source(subtask, workspace, stages_dir)
        evalspace = work_tmp / f"evalspace_{subtask}"
        _safe_copy_tree(src_dir, evalspace, skip_hidden=True)
        validator = validators.get(subtask)
        if validator is None:
            raise RuntimeError(f"Missing validator for {subtask}")
        ok, message = validator(evalspace)
        score = 10.0 if ok else 0.0
        subtask_records.append(
            {
                "subtask": subtask,
                "source_kind": src_kind,
                "score": score,
                "max_score": 10.0,
                "success": bool(ok),
                "message": str(message),
                "passed_60pct": bool(ok),
            }
        )
        if not ok:
            halted = True

    return _aggregate_subtask_scores(subtask_records, scale_max=10.0)


def _aggregate_subtask_scores(
    subtask_records: list[dict[str, Any]],
    *,
    scale_max: float = 10.0,
) -> dict[str, Any]:
    if not subtask_records:
        raise RuntimeError("No subtasks evaluated; refusing to emit default reward.")
    scores = [float(r["score"]) for r in subtask_records]
    mean_raw = sum(scores) / len(scores)
    reward = max(0.0, min(1.0, mean_raw / scale_max))
    s_avg_pct = round(reward * 100.0, 4)
    passed_60 = sum(1 for r in subtask_records if r.get("passed_60pct"))
    return {
        "subtask_count": len(subtask_records),
        "subtask_scores_raw": scores,
        "mean_raw_score": round(mean_raw, 6),
        "scale_max": scale_max,
        "reward": round(reward, 6),
        "s_avg_pct": s_avg_pct,
        "subtasks_passed_60pct": passed_60,
        "subtask_pass_rate_60pct": round(passed_60 / len(subtask_records), 6),
        "scenario_passed_60pct": reward >= 0.6,
        "subtasks": subtask_records,
    }


def evaluate_backend_java_rubric(
    eval_mod: ModuleType,
    description: dict[str, Any],
    workspace: Path,
    stages_dir: Path | None,
    work_tmp: Path,
) -> dict[str, Any]:
    """Replay Backend/scenario2 unaltered CommandRunner + RubricEvaluator."""
    subtask_count = int(description.get("subtask_count") or 0)
    rubric_eval = eval_mod.RubricEvaluator()
    subtask_records: list[dict[str, Any]] = []

    for idx in range(1, subtask_count + 1):
        subtask = f"subtask{idx}"
        src_dir, src_kind = _resolve_stage_source(subtask, workspace, stages_dir)
        evalspace = work_tmp / f"evalspace_{subtask}"
        _safe_copy_tree(
            src_dir,
            evalspace,
            skip_hidden=True,
            excluded_dirs=BACKEND_EXCLUDED_COPY_DIRS,
            excluded_files=BACKEND_EXCLUDED_COPY_FILES,
            excluded_suffixes=BACKEND_EXCLUDED_COPY_SUFFIXES,
        )
        logs_dir = evalspace / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        cmd_runner = eval_mod.CommandRunner(logs_dir, conda_env=None, conda_exe=None)
        commands = cmd_runner.capture(subtask, evalspace)
        rubric_res = rubric_eval.evaluate(subtask, evalspace, commands)
        subtask_records.append(
            {
                "subtask": subtask,
                "source_kind": src_kind,
                "score": float(rubric_res.score),
                "max_score": 10.0,
                "pass_count": rubric_res.pass_count,
                "total_points": rubric_res.total_points,
                "failed_points": list(rubric_res.failed_points),
                "passed_60pct": float(rubric_res.score) >= 6.0,
                "compile_returncode": commands.get("compile", {}).get("returncode"),
            }
        )

    return _aggregate_subtask_scores(subtask_records, scale_max=10.0)


def evaluate_backend_py_rubric(
    eval_mod: ModuleType,
    description: dict[str, Any],
    workspace: Path,
    stages_dir: Path | None,
    work_tmp: Path,
) -> dict[str, Any]:
    """Replay Backend/scenario3 unaltered TestPlanRunner + RubricEvaluator."""
    subtask_count = int(description.get("subtask_count") or 0)
    rubric_eval = eval_mod.RubricEvaluator()
    subtask_records: list[dict[str, Any]] = []

    for idx in range(1, subtask_count + 1):
        subtask = f"subtask{idx}"
        src_dir, src_kind = _resolve_stage_source(subtask, workspace, stages_dir)
        evalspace = work_tmp / f"evalspace_{subtask}"
        _safe_copy_tree(
            src_dir,
            evalspace,
            skip_hidden=True,
            excluded_dirs=frozenset({"__pycache__"}),
        )
        logs_dir = evalspace / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        runner = eval_mod.TestPlanRunner(evalspace, logs_dir, conda_env=None, conda_exe=None)
        rubric_res = rubric_eval.evaluate(subtask, evalspace, runner)
        subtask_records.append(
            {
                "subtask": subtask,
                "source_kind": src_kind,
                "score": float(rubric_res.score),
                "max_score": 10.0,
                "metrics": dict(rubric_res.metrics),
                "notes": list(rubric_res.notes),
                "passed_60pct": float(rubric_res.score) >= 6.0,
            }
        )

    return _aggregate_subtask_scores(subtask_records, scale_max=10.0)


def evaluate_code_loss_single(
    eval_mod: ModuleType,
    description: dict[str, Any],
    workspace: Path,
    stages_dir: Path | None,
    work_tmp: Path,
    upstream_eval_script: Path | None = None,
) -> dict[str, Any]:
    """Replay Code/scenario6 & Code/scenario7 single-subtask loss scoring with anti-spoofing re-evaluation.

    Upstream eval_task.py reads `subtask1_result.json` (requiring `loss`, `equation`, `comment`)
    and maps `loss` via `score_from_loss(loss)`. To prevent agents from writing a fake low loss
    into `subtask1_result.json` without actually modifying `equation.py`, this verifier ALSO
    independently re-executes the immutable pinned `evaluate_equation.py` and `equation.parquet`
    against the agent's `equation.py` and scores `max(reported_loss, measured_loss)`.
    """
    import math
    import subprocess

    subtask = "subtask1"
    src_dir, src_kind = _resolve_stage_source(subtask, workspace, stages_dir)
    evalspace = work_tmp / "evalspace_subtask1"
    _safe_copy_tree(
        src_dir,
        evalspace,
        skip_hidden=True,
        excluded_dirs=frozenset({"__pycache__"}),
    )

    # Restore immutable evaluate_equation.py and equation.parquet if available next to eval_task.py
    if upstream_eval_script is not None:
        pinned_ws = upstream_eval_script.resolve().parent / "workspace"
        for pinned_name in ("evaluate_equation.py", "equation.parquet"):
            pinned_file = pinned_ws / pinned_name
            if pinned_file.is_file():
                shutil.copy2(pinned_file, evalspace / pinned_name)

    result_file = evalspace / "subtask1_result.json"
    reported_loss: float | None = None
    schema_valid = False
    notes: list[str] = []

    if not result_file.is_file():
        notes.append("subtask1_result.json missing")
    else:
        try:
            payload = json.loads(result_file.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                raw_loss = payload.get("loss")
                eq_str = payload.get("equation")
                comment_str = payload.get("comment")
                if raw_loss is not None:
                    val = float(raw_loss)
                    if math.isfinite(val) and val >= 0.0:
                        reported_loss = val
                if (
                    reported_loss is not None
                    and isinstance(eq_str, str)
                    and eq_str.strip()
                    and isinstance(comment_str, str)
                    and comment_str.strip()
                ):
                    schema_valid = True
                else:
                    notes.append("subtask1_result.json missing required finite loss/equation/comment fields")
            else:
                notes.append("subtask1_result.json is not a JSON object")
        except Exception as exc:
            notes.append(f"Failed to parse subtask1_result.json: {exc}")

    measured_loss: float | None = None
    eval_eq_script = evalspace / "evaluate_equation.py"
    eq_py = evalspace / "equation.py"
    if eval_eq_script.is_file() and eq_py.is_file():
        try:
            py_exe = os.environ.get("AGENCYBENCH_PYTHON") or shutil.which("python3") or sys.executable
            proc = subprocess.run(
                [py_exe, str(eval_eq_script)],
                cwd=str(evalspace),
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
            if proc.returncode == 0:
                lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
                if lines:
                    cand = float(lines[-1])
                    if math.isfinite(cand) and cand >= 0.0:
                        measured_loss = cand
            else:
                notes.append(f"evaluate_equation.py exited with code {proc.returncode}")
        except Exception as exc:
            notes.append(f"evaluate_equation.py execution failed: {exc}")
    else:
        notes.append("equation.py or evaluate_equation.py missing")

    effective_loss: float | None = None
    if schema_valid and reported_loss is not None and measured_loss is not None:
        effective_loss = max(reported_loss, measured_loss)

    score = float(eval_mod.score_from_loss(effective_loss))
    subtask_records = [
        {
            "subtask": subtask,
            "source_kind": src_kind,
            "score": score,
            "max_score": 10.0,
            "reported_loss": reported_loss,
            "measured_loss": measured_loss,
            "effective_loss": effective_loss,
            "schema_valid": schema_valid,
            "notes": notes,
            "passed_60pct": score >= 6.0,
        }
    ]
    return _aggregate_subtask_scores(subtask_records, scale_max=10.0)


def evaluate_research_rubric(
    eval_mod: ModuleType,
    description: dict[str, Any],
    workspace: Path,
    stages_dir: Path | None,
    work_tmp: Path,
) -> dict[str, Any]:
    """Replay Research/scenario2 & Research/scenario3 unaltered deterministic RubricEvaluator."""
    subtask_count = int(description.get("subtask_count") or 0)
    rubric_eval = eval_mod.RubricEvaluator()
    subtask_records: list[dict[str, Any]] = []

    for idx in range(1, subtask_count + 1):
        subtask = f"subtask{idx}"
        src_dir, src_kind = _resolve_stage_source(subtask, workspace, stages_dir)
        evalspace = work_tmp / f"evalspace_{subtask}"
        _safe_copy_tree(
            src_dir,
            evalspace,
            skip_hidden=True,
            excluded_dirs=frozenset({"__pycache__"}),
        )
        rubric_res = rubric_eval.evaluate(subtask, evalspace)
        subtask_records.append(
            {
                "subtask": subtask,
                "source_kind": src_kind,
                "score": float(rubric_res.score),
                "max_score": 10.0,
                "notes": list(getattr(rubric_res, "notes", []) or []),
                "passed_60pct": float(rubric_res.score) >= 6.0,
            }
        )

    return _aggregate_subtask_scores(subtask_records, scale_max=10.0)


def verify_scenario(
    *,
    runner_family: str,
    upstream_eval_script: Path,
    description_path: Path,
    workspace: Path,
    stages_dir: Path | None = None,
) -> dict[str, Any]:
    """Run independent verification for one scenario and return structured result."""
    description = json.loads(description_path.read_text(encoding="utf-8"))
    eval_mod = load_upstream_module(
        upstream_eval_script,
        module_name=f"agencybench_upstream_{runner_family}_{abs(hash(str(upstream_eval_script)))}",
    )
    default_tmp = Path(tempfile.gettempdir()).resolve()
    if any(part.startswith(".") for part in default_tmp.parts):
        # Upstream Backend/scenario2 compile_java filters `any(p.startswith(".") for p in path.parts)`
        # on full paths; use a non-dot directory next to workspace instead of shared host /tmp.
        non_dot_parent = workspace.resolve().parent
        if any(part.startswith(".") for part in non_dot_parent.parts):
            non_dot_parent = Path(os.environ.get("AGENCYBENCH_TMPDIR", "/tmp"))
        non_dot_parent.mkdir(parents=True, exist_ok=True)
        tmp_base: str | None = str(non_dot_parent)
    else:
        tmp_base = None
    with tempfile.TemporaryDirectory(prefix="agencybench_verify_", dir=tmp_base) as tmp:
        work_tmp = Path(tmp)
        if runner_family == "backend_cpp_rubric":
            result = evaluate_backend_cpp_rubric(eval_mod, description, workspace, stages_dir, work_tmp)
        elif runner_family == "backend_java_rubric":
            result = evaluate_backend_java_rubric(eval_mod, description, workspace, stages_dir, work_tmp)
        elif runner_family == "backend_py_rubric":
            result = evaluate_backend_py_rubric(eval_mod, description, workspace, stages_dir, work_tmp)
        elif runner_family == "code_mse_rubric":
            result = evaluate_code_mse_rubric(eval_mod, description, workspace, stages_dir, work_tmp)
        elif runner_family == "code_loss_single":
            result = evaluate_code_loss_single(
                eval_mod,
                description,
                workspace,
                stages_dir,
                work_tmp,
                upstream_eval_script=upstream_eval_script,
            )
        elif runner_family == "mcp_filesystem":
            result = evaluate_mcp_filesystem(eval_mod, description, workspace, stages_dir, work_tmp)
        elif runner_family == "research_rubric":
            result = evaluate_research_rubric(eval_mod, description, workspace, stages_dir, work_tmp)
        else:
            raise ValueError(f"Unsupported runner_family for verification: {runner_family}")

    result["runner_family"] = runner_family
    result["status"] = "ok"
    result["error_type"] = None
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Independent AgencyBench V2 Harbor verifier")
    parser.add_argument("--runner-family", required=True)
    parser.add_argument("--eval-script", type=Path, required=True)
    parser.add_argument("--description", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, default=Path("/workspace"))
    parser.add_argument("--stages-dir", type=Path, default=Path("/tmp/.agencybench_stages"))
    parser.add_argument("--reward-txt", type=Path, default=Path("/logs/verifier/reward.txt"))
    parser.add_argument("--reward-json", type=Path, default=Path("/logs/verifier/reward.json"))
    parser.add_argument(
        "--report-json",
        type=Path,
        default=Path("/logs/verifier/agencybench_report.json"),
    )
    args = parser.parse_args()

    args.reward_txt.parent.mkdir(parents=True, exist_ok=True)
    args.reward_json.parent.mkdir(parents=True, exist_ok=True)
    args.report_json.parent.mkdir(parents=True, exist_ok=True)

    stages_dir = args.stages_dir if args.stages_dir and args.stages_dir.is_dir() else None
    try:
        result = verify_scenario(
            runner_family=args.runner_family,
            upstream_eval_script=args.eval_script,
            description_path=args.description,
            workspace=args.workspace,
            stages_dir=stages_dir,
        )
        harbor_rewards = {
            "reward": float(result["reward"]),
        }
        args.reward_txt.write_text(f"{result['reward']:.6f}\n", encoding="utf-8")
        args.reward_json.write_text(json.dumps(harbor_rewards, indent=2) + "\n", encoding="utf-8")
        args.report_json.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(
            f"[VERIFIER] {args.runner_family}: reward={result['reward']:.6f} "
            f"(S_avg={result['s_avg_pct']:.2f}%, raw={result['subtask_scores_raw']})"
        )
    except VerifierSecurityError as exc:
        err_payload = {
            "status": "error",
            "error_type": "security_violation",
            "error_message": str(exc),
            "reward": 0.0,
        }
        args.reward_txt.write_text("0.000000\n", encoding="utf-8")
        args.reward_json.write_text(json.dumps({"reward": 0.0}, indent=2) + "\n", encoding="utf-8")
        args.report_json.write_text(json.dumps(err_payload, indent=2) + "\n", encoding="utf-8")
        print(f"[VERIFIER][SECURITY_ERROR] {exc}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        err_payload = {
            "status": "error",
            "error_type": "verifier_crash",
            "error_message": str(exc),
            "traceback": traceback.format_exc(),
        }
        # Fail closed: do NOT write reward.txt or reward.json on verifier infrastructure crash.
        args.report_json.write_text(json.dumps(err_payload, indent=2) + "\n", encoding="utf-8")
        print(f"[VERIFIER][CRASH] {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
