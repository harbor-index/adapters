"""Evaluate the submitted answer with the upstream reference grader."""

import json
from pathlib import Path
from runpy import run_path

# Isolated Python ignores agent-controlled environment and import directories.
evaluate = run_path(str(Path(__file__).with_name("reference_grader.py")))["evaluate"]


def main() -> int:
    config = json.loads(Path("/tests/eval_config.json").read_text())
    try:
        submission = json.loads(Path("/workspace/eval_answer.json").read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        result = {"passed": False, "submission_error": str(exc)}
    else:
        if not isinstance(submission, dict):
            result = {"passed": False, "submission_error": "Expected a JSON object"}
        else:
            result = evaluate(config, submission)

    Path("/logs/verifier/result.json").write_text(
        json.dumps(result, indent=2, allow_nan=False) + "\n"
    )
    passed = result["passed"]
    Path("/logs/verifier/reward.txt").write_text("1\n" if passed else "0\n")
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
