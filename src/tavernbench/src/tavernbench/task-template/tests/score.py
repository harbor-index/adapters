"""Score one TavernBench answer against its label.

    python3 score.py --answer /app/answer.json --label /tests/label.json \
        --details /logs/verifier/score.json

The exit status is 0 when the answer is correct and 1 otherwise. ``--details``
receives a JSON record with the binary result (``correct``), the graded score in
[0, 1] (``score``) and the parsed answer.

``parse`` and ``score_item`` are ported without change in behaviour from
``tavernbench/tavernbench/score.py`` in https://github.com/MCG-NJU/AutoTavern
(tag v1.0.0, MIT License, Copyright (c) 2026 The AutoTavern Authors):

  exact       answer equals the label (lists compared exactly, strings
              case-insensitively)
  set         the answer's elements equal the label's, ignoring order and case
  brier       probability triple; graded score = 1 - Brier/2, correct if the
              most likely outcome matches and Brier <= 0.1
  numeric_rel correct if |answer - label| <= max(1, tol * |label|)
  numeric_abs correct if |answer - label| <= tol
  value_map, value_map_str, value_json
              the answer is a key of ``values``; graded score = value / best,
              correct if within ``tol`` of the best value

An answer that is missing or cannot be parsed scores 0. The original scorer also
zeroes an API call that was cut off or that spent more than 4,096 output tokens;
those checks describe a single model call and have no counterpart for a file
written by an agent, so they are not applied here.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path


def parse(resp):
    """Extract the JSON object from an answer, tolerating a Markdown code fence."""
    if resp is None:
        return None
    s = resp.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def score_item(label: dict, obj) -> tuple[int, float]:
    """Return (correct, graded score) for a parsed answer."""
    if obj is None:
        return 0, 0.0
    kind = label["scoring"]
    try:
        if kind == "exact":
            a, b = obj.get("answer"), label["answer"]
            if isinstance(b, str):
                ok = isinstance(a, str) and a.strip().lower() == b.lower()
            else:
                ok = a == b
            return int(ok), float(ok)
        if kind == "brier":
            p = {k: float(obj.get(k, 0)) for k in ("win", "tie", "loss")}
            z = sum(p.values()) or 1.0
            p = {k: v / z for k, v in p.items()}
            q = label["answer"]
            brier = sum((p[k] - q[k]) ** 2 for k in q)
            ok = max(p, key=p.get) == max(q, key=q.get) and brier <= 0.1
            return int(ok), 1 - brier / 2
        if kind == "numeric_rel":
            a, b = float(obj.get("answer")), float(label["answer"])
            err = abs(a - b)
            ok = err <= max(1.0, label.get("tol", 0.25) * abs(b))
            return int(ok), 1 - min(1.0, err / max(1.0, abs(b)))
        if kind == "set":
            got = {str(x).strip().upper() for x in obj.get("answer", [])}
            ok = got == {str(x).upper() for x in label["answer"]}
            return int(ok), float(ok)
        if kind == "numeric_abs":
            err = abs(float(obj.get("answer")) - float(label["answer"]))
            return int(err <= label.get("tol", 0.1)), 1 - min(1.0, err)
        if kind == "value_map_str":
            key = (
                str(obj.get("answer", ""))
                .strip()
                .upper()
                .replace(",", "")
                .replace(" ", "")
            )
            v = label["values"].get(key)
            if v is None:
                return 0, 0.0
            best = label["answer"]
            return int(v >= best - label.get("tol", 0)), (v / best if best else 1.0)
        if kind == "value_json":
            v = label["values"].get(json.dumps(obj.get("answer")))
            if v is None:
                return 0, 0.0
            best = label["answer"]
            return int(v >= best - label.get("tol", 0)), (v / best if best else 1.0)
        if kind == "value_map":
            key = ",".join(str(int(x)) for x in obj.get("answer", []))
            v = label["values"].get(key)
            if v is None:
                return 0, 0.0
            best = label["answer"]
            return int(v >= best - label.get("tol", 0)), (v / best if best else 1.0)
    except (TypeError, ValueError, AttributeError):
        return 0, 0.0
    raise ValueError(kind)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--answer", type=Path, required=True, help="File written by the agent"
    )
    parser.add_argument(
        "--label", type=Path, required=True, help="TavernBench label of this task"
    )
    parser.add_argument(
        "--details", type=Path, default=None, help="Where to write the score record"
    )
    args = parser.parse_args()

    label = json.loads(args.label.read_text(encoding="utf-8"))

    status = "scored"
    parsed = None
    try:
        text = args.answer.read_text(encoding="utf-8")
    except FileNotFoundError:
        status, text = "answer file not found", None
    except (OSError, UnicodeDecodeError) as exc:
        status, text = f"answer file unreadable: {exc}", None
    if text is not None:
        parsed = parse(text)
        if parsed is None:
            status = "no JSON object found in the answer file"

    correct, score = score_item(label, parsed)
    if not math.isfinite(
        score
    ):  # e.g. NaN probabilities; such an answer is never correct
        correct, score = 0, 0.0

    record = {
        "id": label.get("id"),
        "scoring": label["scoring"],
        "status": status,
        "correct": correct,
        "score": round(score, 6),
        "answer": parsed,
    }
    print(json.dumps(record, ensure_ascii=False))
    if args.details is not None:
        args.details.parent.mkdir(parents=True, exist_ok=True)
        args.details.write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return 0 if correct else 1


if __name__ == "__main__":
    sys.exit(main())
