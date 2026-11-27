"""Verifier for MedXpertQA-MM Harbor tasks.

``answer_cleansing`` and ``set_correctness`` below are ported from the original
benchmark (TsinghuaC3I/MedXpertQA, ``eval/utils.py`` and ``eval/main.py``),
keeping only the branches that the MedXpertQA dataset reaches under the
default zero-shot setting. The extraction regex, phrase stripping,
first-match rule and case-insensitive comparison are unchanged.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

# config/prompt_templates.py -> zero_shot_cot_trigger for medxpertqa
ANSWER_TRIGGER = "Therefore, among {start} through {end}, the answer is"


def answer_cleansing(answer_trigger: str, prediction: str, input_sample: dict) -> list:
    if (
        "{start}" in answer_trigger
        and "{end}" in answer_trigger
        and "options" in input_sample
    ):
        options_num = len(input_sample["options"])
        answer_trigger = answer_trigger.format(start="A", end=chr(65 + options_num - 1))

    # method == "zero_shot"
    try:
        prediction = prediction.split(answer_trigger)[1].strip()
    except IndexError:
        try:
            prediction = prediction.split(" answer is ")[1].strip()
        except IndexError:
            pass

    for unwanted_phrase in [
        "I understand",
        "A through J",
        "A through E",
        "A through D",
    ]:
        prediction = prediction.replace(unwanted_phrase, "")

    options_num = len(input_sample["options"])
    options = [chr(65 + i) for i in range(options_num)]
    options_str = r"\b(" + "|".join(options) + r")\b"
    prediction = re.findall(options_str, prediction)

    if len(prediction) == 0:
        prediction = []
    else:
        if "label" in input_sample and len(input_sample["label"]) == 1:
            # zero_shot: choose the first element in list
            prediction = [prediction[0]]
            if prediction[0] and prediction[0].endswith("."):
                prediction[0] = prediction[0][:-1]

    return prediction


def set_correctness(label: list, prediction: list) -> bool:
    if len(prediction) == 0:
        return False
    if len(label) == 1:
        return label[0].lower() == prediction[0].lower()
    return len(label) == len(prediction) and all(
        lab == pred for lab, pred in zip(label, prediction)
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--answer-file", type=Path, required=True)
    parser.add_argument("--ground-truth", type=Path, required=True)
    parser.add_argument("--reward-file", type=Path, required=True)
    args = parser.parse_args()

    sample = json.loads(args.ground_truth.read_text(encoding="utf-8"))

    if args.answer_file.is_file():
        response = args.answer_file.read_text(
            encoding="utf-8", errors="replace"
        ).strip()
    else:
        print(f"Answer file not found: {args.answer_file}")
        response = ""

    prediction = answer_cleansing(ANSWER_TRIGGER, response, sample)
    correct = set_correctness(sample["label"], prediction)

    print(f"id: {sample['id']}")
    print(f"label: {sample['label']}")
    print(f"prediction: {prediction}")
    print(f"correct: {correct}")

    args.reward_file.parent.mkdir(parents=True, exist_ok=True)
    args.reward_file.write_text("1\n" if correct else "0\n")


if __name__ == "__main__":
    main()
