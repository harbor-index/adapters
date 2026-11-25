"""Verifier entry point: score /app/answer.txt against the reference answer."""

import json
import sys
from pathlib import Path

TESTS_DIR = Path("/tests")
sys.path.insert(0, str(TESTS_DIR))  # makes /tests/SEED importable as package `SEED`

from cmphysbench_answer import score_response  # noqa: E402

ANSWER_FILE = Path("/app/answer.txt")
LOG_DIR = Path("/logs/verifier")


def main() -> int:
    truth = json.loads((TESTS_DIR / "ground_truth.json").read_text(encoding="utf-8"))
    if ANSWER_FILE.is_file():
        response = ANSWER_FILE.read_text(encoding="utf-8", errors="replace")
        answer_found = True
    else:
        # No submission is a genuine agent failure and scores 0, as upstream
        # scores a response without a boxed answer.
        response = ""
        answer_found = False

    result = score_response(truth["answer"], response, truth["answer_type"])
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    rewards = {k: result[k] for k in ("reward", "seed_score", "accuracy")}
    (LOG_DIR / "reward.json").write_text(json.dumps(rewards), encoding="utf-8")
    (LOG_DIR / "reward.txt").write_text(str(rewards["reward"]), encoding="utf-8")
    details = {
        "answer_file_found": answer_found,
        "extracted_answer": result["extracted_answer"],
        "reference_answer": truth["answer"],
        "answer_type": truth["answer_type"],
        **rewards,
    }
    (LOG_DIR / "details.json").write_text(json.dumps(details, indent=2), encoding="utf-8")
    print(json.dumps(details, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
