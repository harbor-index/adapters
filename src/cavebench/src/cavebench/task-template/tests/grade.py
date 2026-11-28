"""Grade a CaveBench answer with the upstream scorer.

contracts.py is vendored verbatim from PIER-Lab/CaveBench. The answer file's
text is scored exactly as upstream scores a model response: it must contain one
complete, legal JSON object whose sole key is "operations".
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from contracts import score  # noqa: E402


def grade(answer_text: str, expected: dict) -> tuple[dict, dict]:
    """Return (rewards, details) for one answer text."""
    question = {key: expected[key] for key in ("id", "k", "allowed_labels", "candidates")}
    result = score(answer_text, question, expected["operations"])
    flags = result["step_correct"]
    rewards = {
        "reward": 1.0 if result["all_steps_correct"] else 0.0,
        "event_accuracy": sum(flags) / len(flags),
        "format_valid": 1.0 if result["format_valid"] else 0.0,
    }
    return rewards, result


def main() -> None:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--answer", type=Path, required=True)
    cli.add_argument("--expected", type=Path, required=True)
    cli.add_argument("--out", type=Path, required=True)
    args = cli.parse_args()

    expected = json.loads(args.expected.read_text(encoding="utf-8"))
    if args.answer.is_file():
        answer_text = args.answer.read_text(encoding="utf-8", errors="replace")
    else:
        print(f"no answer file at {args.answer}; scoring an empty response")
        answer_text = ""

    rewards, result = grade(answer_text, expected)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "reward.json").write_text(json.dumps(rewards), encoding="utf-8")
    details = {
        "id": expected["id"],
        "answer_file_present": args.answer.is_file(),
        "parsed_operations": result["operations"],
        "reference_operations": expected["operations"],
        "step_correct": result["step_correct"],
        **rewards,
    }
    (args.out / "grading.json").write_text(json.dumps(details, indent=2), encoding="utf-8")
    print(json.dumps(rewards))


if __name__ == "__main__":
    main()
