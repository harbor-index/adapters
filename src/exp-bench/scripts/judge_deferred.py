"""Grade Harbor trials that ran with EXP_BENCH_DEFER_JUDGE=1.

With EXP_BENCH_DEFER_JUDGE=1 the verifier captures everything that needs the task
container (agent output, git patch, agent logs, execution check) in
``verifier/judge_inputs.json`` and skips the LLM calls. This script runs the same
judge code (``task-template/tests/judge.py``: monitor, design/conclusion, chunked setup
judge) on those inputs and writes ``eval_judge.json``, ``monitor.json``,
``reward.json`` and ``reward.txt`` into each trial's ``verifier/`` directory, then
prints the mean of every metric.

Usage (from src/exp-bench; judge settings come from the environment as in task.toml):
    OPENAI_API_KEY=... [OPENAI_BASE_URL=...] [EXP_BENCH_JUDGE_MODEL=o3-mini] \
    uv run --with openai==1.66.3 --with tiktoken==0.9.0 --with langchain-text-splitters==0.3.6 \
        python scripts/judge_deferred.py <job-dir> [--force]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import statistics
from pathlib import Path

JUDGE_PY = Path(__file__).resolve().parents[1] / "src" / "exp_bench" / "task-template" / "tests" / "judge.py"


def load_judge():
    spec = importlib.util.spec_from_file_location("exp_bench_judge", JUDGE_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("job_dir", type=Path)
    parser.add_argument("--force", action="store_true", help="Re-grade trials that already have eval_judge.json")
    args = parser.parse_args()

    judge = load_judge()
    results = {}
    for inputs_path in sorted(args.job_dir.glob("*/verifier/judge_inputs.json")):
        verifier_dir = inputs_path.parent
        trial = verifier_dir.parent.name
        if (verifier_dir / "eval_judge.json").exists() and not args.force:
            results[trial] = json.loads((verifier_dir / "reward.json").read_text())
            continue
        inputs = json.loads(inputs_path.read_text())
        if inputs["judge_model"] != judge.JUDGE_MODEL:
            print(f"WARNING {trial}: inputs captured for {inputs['judge_model']}, grading with {judge.JUDGE_MODEL}")
        judge.VERIFIER_DIR = verifier_dir
        judge.ORACLE_VALIDATION = inputs["oracle_validation"]
        judge.EXEC_CHECK = inputs["exec_check"]
        print(f"grading {trial}", flush=True)
        results[trial] = judge.grade(
            inputs["spec"],
            inputs["output_json"],
            inputs["patch"],
            agent_logs=inputs["agent_logs"],
            execution=inputs["execution"],
        )

    if not results:
        print(f"No trials with verifier/judge_inputs.json under {args.job_dir}")
        return
    (args.job_dir / "deferred_judge_results.json").write_text(json.dumps(results, indent=2))
    metrics = sorted({k for r in results.values() for k in r if k != "judge_pending"})
    print(f"\n{len(results)} trials (judge model {os.environ.get('EXP_BENCH_JUDGE_MODEL') or 'o3-mini'})")
    for metric in metrics:
        values = [r[metric] for r in results.values() if metric in r]
        print(f"  {metric:32s} mean {statistics.mean(values):.4f}  (n={len(values)})")


if __name__ == "__main__":
    main()
