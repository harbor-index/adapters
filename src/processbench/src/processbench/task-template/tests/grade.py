"""Grade a ProcessBench answer.

Answer extraction and matching mirror the original evaluation
(QwenLM/ProcessBench code/run_eval.py: extract_answer + int() + exact match).
"""

import json
import re
from pathlib import Path

ANSWER_PATH = Path("/app/answer.txt")
GROUND_TRUTH_PATH = Path("/tests/ground_truth.json")
VERIFIER_DIR = Path("/logs/verifier")


def extract_answer(solution_text: str):
    boxed_pattern = r"\\boxed\{([^}]*)\}"
    matches = re.findall(boxed_pattern, solution_text)
    if matches:
        return matches[-1].strip()
    return None


def main() -> None:
    gt = json.loads(GROUND_TRUTH_PATH.read_text())

    if ANSWER_PATH.is_file():
        response = ANSWER_PATH.read_text(errors="replace")
    else:
        print(f"{ANSWER_PATH} not found")
        response = ""

    pred = extract_answer(response)
    try:
        pred = int(pred)
    except (TypeError, ValueError):
        pred = None

    match = pred == gt["label"]
    print(f"label={gt['label']} prediction={pred} match={match}")

    VERIFIER_DIR.mkdir(parents=True, exist_ok=True)
    (VERIFIER_DIR / "metrics.json").write_text(
        json.dumps({**gt, "prediction": pred, "match": match}) + "\n"
    )
    (VERIFIER_DIR / "reward.txt").write_text("1\n" if match else "0\n")


if __name__ == "__main__":
    main()
