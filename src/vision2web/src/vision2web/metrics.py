"""Aggregate upstream per-prototype and per-case values, preserving their denominators."""

from __future__ import annotations

import math

LEVELS = ("webpage", "frontend", "website")


def mean(values):
    values = list(values)
    if any(
        not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1 for v in values
    ):
        raise ValueError("Invalid non-finite/out-of-range upstream score")
    return sum(values) / len(values) if values else 0.0


def task_reward(record: dict) -> float:
    visual = mean(record["prototypes"].values())
    if record["level"] == "webpage":
        return visual
    return (visual + mean(record["function"].values())) / 2


def aggregate(records: list[dict]) -> dict:
    ids = [r["id"] for r in records]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate task records")
    summary = {}
    for level in LEVELS:
        rows = [r for r in records if r["level"] == level]
        if not rows:
            continue
        prototypes = [v for r in rows for v in r["prototypes"].values()]
        functions = [v for r in rows for v in r["function"].values()]
        vs, fs = mean(prototypes), mean(functions)
        summary[level] = {
            "tasks": len(rows),
            "prototype_count": len(prototypes),
            "functional_count": len(functions),
            "visual": vs,
            "functional": fs,
            "score": vs if level == "webpage" else (vs + fs) / 2,
        }
    return {
        "scope": "Only the supplied records; report coverage with any score.",
        "levels": summary,
        "overall": sum(summary[x]["score"] for x in LEVELS) / 3
        if all(x in summary for x in LEVELS)
        else None,
    }
