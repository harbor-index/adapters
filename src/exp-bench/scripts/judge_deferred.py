"""Grade Harbor trials that ran with EXP_BENCH_DEFER_JUDGE=1.

With EXP_BENCH_DEFER_JUDGE=1 the verifier captures everything that needs the task
container (agent output, git patch, agent logs, execution check) in
``verifier/judge_inputs.json`` and skips the LLM calls. This script runs the same
judge code (``task-template/tests/judge.py``: monitor, design/conclusion, chunked setup
judge) on those inputs. Results for each judge model go to
``verifier/deferred/<judge-model>/`` (``eval_judge.json``, ``monitor.json``,
``reward.json``), so one job can be graded by several judges; the per-trial rewards are
collected in ``<job-dir>/deferred_judge_results_<judge-model>.json`` and the mean of every
metric is printed.

When the judge model differs from the one the inputs were captured for, the agent logs
are re-read from the trial's ``agent/`` directory so they are truncated to the new
judge's context rather than the old one's.

Usage (from src/exp-bench; judge settings come from the environment as in task.toml):
    OPENAI_API_KEY=... [OPENAI_BASE_URL=...] [EXP_BENCH_JUDGE_MODEL=o3-mini] \
    uv run --with openai==1.66.3 --with tiktoken==0.9.0 --with langchain-text-splitters==0.3.6 \
        python scripts/judge_deferred.py <job-dir> [<job-dir> ...] [--force]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
from pathlib import Path

JUDGE_PY = Path(__file__).resolve().parents[1] / "src" / "exp_bench" / "task-template" / "tests" / "judge.py"


def load_judge():
    spec = importlib.util.spec_from_file_location("exp_bench_judge", JUDGE_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def grade_job(judge, job_dir: Path, force: bool) -> dict:
    results = {}
    for inputs_path in sorted(job_dir.glob("*/verifier/judge_inputs.json")):
        trial_dir = inputs_path.parents[1]
        out_dir = inputs_path.parent / "deferred" / judge.JUDGE_MODEL
        if (out_dir / "reward.json").exists() and not force:
            results[trial_dir.name] = json.loads((out_dir / "reward.json").read_text())
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        inputs = json.loads(inputs_path.read_text())
        judge.VERIFIER_DIR = out_dir
        judge.ORACLE_VALIDATION = inputs["oracle_validation"]
        judge.EXEC_CHECK = inputs["exec_check"]
        agent_logs = inputs["agent_logs"]
        if (
            not inputs["oracle_validation"]
            and inputs["judge_model"] != judge.JUDGE_MODEL
            and (trial_dir / "agent").is_dir()
        ):
            judge.AGENT_LOG_DIR = trial_dir / "agent"
            agent_logs = judge.read_agent_logs()
        print(f"grading {job_dir.name}/{trial_dir.name} with {judge.JUDGE_MODEL}", flush=True)
        try:
            results[trial_dir.name] = judge.grade(
                inputs["spec"],
                inputs["output_json"],
                inputs["patch"],
                agent_logs=agent_logs,
                execution=inputs["execution"],
            )
        except Exception as e:  # keep grading the other trials
            print(f"  FAILED: {type(e).__name__}: {str(e)[:300]}")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("job_dirs", type=Path, nargs="+")
    parser.add_argument("--force", action="store_true", help="Re-grade trials already graded by this judge model")
    args = parser.parse_args()

    judge = load_judge()
    for job_dir in args.job_dirs:
        results = grade_job(judge, job_dir, args.force)
        if not results:
            print(f"No graded trials under {job_dir}")
            continue
        out = job_dir / f"deferred_judge_results_{judge.JUDGE_MODEL}.json"
        out.write_text(json.dumps(results, indent=2))
        metrics = sorted({k for r in results.values() for k in r})
        print(f"\n{job_dir.name}: {len(results)} trials, judge {judge.JUDGE_MODEL} -> {out.name}")
        for metric in metrics:
            values = [r[metric] for r in results.values() if metric in r]
            print(f"  {metric:32s} mean {statistics.mean(values):.4f}  (n={len(values)})")


if __name__ == "__main__":
    main()
