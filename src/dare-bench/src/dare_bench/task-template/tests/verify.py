"""Score /workspace/prediction.csv with DARE-Bench's own evaluator.

Instruction-following (tabular v1) labels are produced here, the same way upstream does with
force_simulate=true: run the reference-solution code generated from the task metadata on the
clean training split, inside the same environment as the agent. All other variants compare
against the released ground-truth file.

Exit 0 means a reward was written, even when the agent's output is missing or malformed (that
scores 0, matching upstream, where such runs count as 0 in the reported average). Exit 1 means
the benchmark fixture itself failed, which is an infrastructure error.
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_reference_label(tests: Path, spec: dict, logs: Path) -> Path:
    """Run upstream's reference-solution code on the hidden clean split; return the label path."""
    work = Path(tempfile.mkdtemp(prefix="dare_reference_"))
    for name in spec["reference_inputs"]:
        shutil.copy(tests / "reference" / name, work / name)
    shutil.copy(tests / "reference" / "reference_solution.py", work / "reference_solution.py")
    proc = subprocess.run(
        [sys.executable, "reference_solution.py"], cwd=work, capture_output=True, text=True, timeout=600
    )
    (logs / "reference_run.log").write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr)
    label = work / "simulated_pred_local.csv"
    if proc.returncode != 0 or not label.is_file():
        raise RuntimeError(f"reference solution failed (exit {proc.returncode}); see reference_run.log")
    return label


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--tests", type=Path, required=True)
    parser.add_argument("--logs", type=Path, required=True)
    args = parser.parse_args()

    sys.path.insert(0, str(args.tests))
    from dare_evaluation import evaluate_prediction  # verbatim copy of upstream scripts/evaluation.py

    spec = json.loads((args.tests / "spec.json").read_text())
    metadata_path = args.tests / "metadata.json"
    details = {"task_id": spec["task_id"], "variant": spec["variant"], "metric": spec["metric"]}

    try:
        if spec["label"] == "reference":
            label = build_reference_label(args.tests, spec, args.logs)
        else:
            label = args.tests / "labels" / spec["label"]
        details["label_sha256"] = sha256(label)
    except Exception as exc:  # the fixture is broken, not the agent's output
        details["infrastructure_error"] = repr(exc)
        (args.logs / "verifier_result.json").write_text(json.dumps(details, indent=1))
        print(f"Infrastructure error: {exc!r}", file=sys.stderr)
        return 1

    prediction = args.workspace / "prediction.csv"
    if prediction.is_file():
        details["prediction_sha256"] = sha256(prediction)
    try:
        result = evaluate_prediction(str(prediction), str(label), str(metadata_path), spec["variant"])
    except Exception as exc:  # unreadable or malformed prediction
        result = {"prediction_exist": 1.0, "final_score": 0.0, "error": f"evaluation_exception: {exc!r}"}

    score = float(result["final_score"])
    details.update(result)
    details["reward"] = score
    (args.logs / "verifier_result.json").write_text(json.dumps(details, indent=1, default=str))
    (args.logs / "reward.txt").write_text(f"{score}\n")
    print(json.dumps(details, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
