"""Grade the agent's /workspace/solution.xlsx with the official MBABench 2.0 judge.

Runs inside the separate verifier container (tests/Dockerfile). The flow is the
upstream coding-agent pipeline's, end to end:

1. Delivery validation, as coding-agents/coding_agent/validate.py: the workbook
   must exist, open with openpyxl, have a sheet, and differ byte-wise from every
   file seeded into the workspace. A rejected delivery scores 0.
2. Recording, as coding-agents/coding_agent/local_sink.write_attempt: one
   attempt row in <output_root>/attempts/<label>/task_attempts.jsonl.
3. Grading with upstream's own judge/main_scripts/grade.py (unmodified, at the
   commit pinned in tests/Dockerfile): deterministic checks, the Questions-sheet
   answer check, then the single-pass LLM judge, with the LibreOffice
   recalculation retry for workbooks saved without cached values.
4. Reward = the grading's recorded total (0-100) / 100.

A grading that cannot be completed (judge/API/LibreOffice failure, or a grading
row marked failed) exits non-zero without writing a reward: upstream re-runs
such gradings instead of scoring them.

Environment:
    MBABENCH_JUDGE_MODEL           grader label (judge/judge_identities.yaml);
                                   default openai/gpt-5.6-sol, upstream's default
    MBABENCH_ORACLE_SHORTCIRCUIT   "1" (default): a delivery byte-identical to the
                                   golden workbook scores 1.0 without a judge call;
                                   "0" grades it with the judge like any attempt
    MBABENCH_AGENT_LABEL           agent_model_name recorded in the attempt row
    OPENAI_API_KEY / OPENAI_BASE_URL   the grader's endpoint (read by the judge)
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

TESTS_DIR = Path("/tests")
SOLUTION = Path("/workspace/solution.xlsx")
VERIFIER_LOGS = Path("/logs/verifier")
JUDGE_REPO = Path(os.environ.get("MBABENCH_ROOT", "/opt/mbabench"))
DATA_ROOT = TESTS_DIR / "data"
OUTPUT_ROOT = Path("/tmp/mbabench-outputs")

DEFAULT_GRADER = "openai/gpt-5.6-sol"
DEFAULT_AGENT_LABEL = "harbor/coding-agent"

# coding-agents/coding_agent/local_sink.ATTEMPT_COLUMNS (identical across the
# four upstream pipelines).
ATTEMPT_COLUMNS = (
    "id",
    "task_id",
    "agent_model_name",
    "agent_model_type",
    "attempt_files",
    "prompt_files",
    "start_time",
    "end_time",
    "time_taken_min",
    "cost",
    "prompt_version",
    "agent_failed",
    "agent_failed_reason",
    "deprecated",
    "created_at",
    "context_reduced",
    "deprecated_reason",
    "updated_at",
    "extra_configs",
)


class GradingError(RuntimeError):
    """The judge could not produce a trustworthy grading."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat()


# ---------------------------------------------------------------- validation


def reject_delivery(solution: Path, seeded_sha256: dict[str, str]) -> str | None:
    """Why upstream's runner would not bank this delivery, or None if it would."""
    if not solution.is_file():
        return "Agent exited without producing solution.xlsx"
    if solution.stat().st_size == 0:
        return "solution.xlsx is empty"
    try:
        import openpyxl

        workbook = openpyxl.load_workbook(solution, read_only=True)
        sheet_count = len(workbook.sheetnames)
        workbook.close()
    except Exception as exc:  # noqa: BLE001 - any load failure means invalid
        return f"solution.xlsx failed to open: {exc!r}"
    if sheet_count == 0:
        return "solution.xlsx has no sheets"
    if sha256_file(solution) in seeded_sha256.values():
        return "solution.xlsx is byte-identical to a seeded input file - not new work"
    return None


# ------------------------------------------------------------------- grading


def record_attempt(config: dict, label: str) -> int:
    """Write the attempt row grade.py selects, with solution.xlsx copied into
    the attempt folder upstream's local sink would use. Returns the row id."""
    shutil.rmtree(OUTPUT_ROOT, ignore_errors=True)
    cohort_dir = OUTPUT_ROOT.joinpath("attempts", *label.split("/"))
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    attempt_dir = cohort_dir / f"task_id={config['task_id']}" / stamp
    attempt_dir.mkdir(parents=True)
    shutil.copy2(SOLUTION, attempt_dir / "solution.xlsx")

    now = _iso_now()
    row = dict.fromkeys(ATTEMPT_COLUMNS)
    row.update(
        id=int(time.time() * 1000),
        task_id=config["task_id"],
        agent_model_name=label,
        agent_model_type="coding_cli",
        attempt_files=[
            (attempt_dir / "solution.xlsx").relative_to(OUTPUT_ROOT).as_posix()
        ],
        prompt_files=[],
        prompt_version=config["prompt_version"],
        agent_failed=False,
        deprecated=False,
        created_at=now,
        updated_at=now,
        extra_configs={"harness": "harbor"},
    )
    with (cohort_dir / "task_attempts.jsonl").open("w", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    return row["id"]


def run_grade_py(grader: str) -> None:
    env = {
        **os.environ,
        "MBABENCH_DATA_ROOT": str(DATA_ROOT),
        "MBABENCH_OUTPUT_ROOT": str(OUTPUT_ROOT),
    }
    command = [
        sys.executable,
        str(JUDGE_REPO / "judge" / "main_scripts" / "grade.py"),
        "--all",
        "--model",
        grader,
    ]
    print(f"[mbabench] running: {' '.join(command)}", flush=True)
    completed = subprocess.run(command, cwd=JUDGE_REPO, env=env, check=False)
    if completed.returncode != 0:
        raise GradingError(f"grade.py exited with code {completed.returncode}")


def load_grading(attempt_id: int) -> dict:
    gradings_file = OUTPUT_ROOT / "gradings" / "gradings.jsonl"
    if not gradings_file.is_file():
        raise GradingError("grade.py wrote no grading (see its log above)")
    rows = [
        json.loads(line)
        for line in gradings_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rows = [r for r in rows if r.get("attempt_id") == attempt_id]
    if len(rows) != 1:
        raise GradingError(
            f"expected one grading for attempt {attempt_id}, found {len(rows)}"
        )
    grading = rows[0]
    if grading.get("failed"):
        raise GradingError(f"grading marked failed: {grading.get('failed_reason')}")
    return grading


def keep_grading_evidence(grading: dict) -> None:
    """Copy the grading's small artifacts (scores, verdicts, det checks, judge
    conversation) into the verifier logs; skip the bulky CSV exports."""
    source = OUTPUT_ROOT / str(grading.get("raw_files_path") or "")
    if not source.is_dir():
        return
    dest = VERIFIER_LOGS / "grading"
    dest.mkdir(parents=True, exist_ok=True)
    for path in source.iterdir():
        if path.is_file():
            shutil.copy2(path, dest / path.name)
        elif path.name == "judge_conversation_logs":
            shutil.copytree(path, dest / path.name, dirs_exist_ok=True)
    (dest / "grading_row.json").write_text(json.dumps(grading, indent=2, default=str))


# -------------------------------------------------------------------- output


def finish(reward: float, summary: dict) -> int:
    VERIFIER_LOGS.mkdir(parents=True, exist_ok=True)
    summary = {"reward": reward, **summary}
    (VERIFIER_LOGS / "mbabench_result.json").write_text(
        json.dumps(summary, indent=2) + "\n"
    )
    (VERIFIER_LOGS / "reward.txt").write_text(f"{reward}\n")
    print(f"[mbabench] {json.dumps(summary)}", flush=True)
    return 0


def main() -> int:
    config = json.loads((TESTS_DIR / "task_config.json").read_text(encoding="utf-8"))
    grader = os.environ.get("MBABENCH_JUDGE_MODEL") or DEFAULT_GRADER
    label = os.environ.get("MBABENCH_AGENT_LABEL") or DEFAULT_AGENT_LABEL

    rejection = reject_delivery(SOLUTION, config["seeded_sha256"])
    if rejection:
        return finish(0.0, {"status": "agent_failure", "reason": rejection})

    shortcircuit = os.environ.get("MBABENCH_ORACLE_SHORTCIRCUIT", "1") != "0"
    if shortcircuit and sha256_file(SOLUTION) == config["golden_sha256"]:
        return finish(
            1.0, {"status": "oracle", "reason": "delivery is the golden workbook"}
        )

    attempt_id = record_attempt(config, label)
    try:
        run_grade_py(grader)
        grading = load_grading(attempt_id)
    except GradingError as exc:
        print(f"[mbabench] grading failed, no reward written: {exc}", file=sys.stderr)
        return 1
    keep_grading_evidence(grading)

    scored = grading["scored_results"]
    total = float(scored["total_score"])
    return finish(
        total / 100.0,
        {
            "status": "graded",
            "total_score": total,
            "criteria_scores": {
                name: block.get("normalized_score")
                for name, block in (scored.get("criteria_scores") or {}).items()
            },
            "grader_model": grading["grader_model"],
            "grader_reasoning": grading.get("grader_reasoning"),
            "judge_version": grading.get("judge_version"),
            "prompt_version": grading.get("prompt_version"),
            "cost_usd": grading.get("cost"),
            "time_elapsed_min": grading.get("time_elapsed_min"),
        },
    )


if __name__ == "__main__":
    sys.exit(main())
