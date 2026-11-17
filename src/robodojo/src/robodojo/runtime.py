"""Validation shared by the simulator supervisor, the verifier and aggregation.

Rewards are derived only from native simulator result files. Infrastructure
errors and incomplete runs raise ``EvaluationError`` and produce no reward.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any

from .catalog import mock_task_spec, task_spec


class EvaluationError(RuntimeError):
    """A run cannot yield a trustworthy, complete native evaluation result."""


class ResultsDirectoryError(EvaluationError):
    """Startup failed before this process owned its results directory."""


def reserve_results(directory: Path) -> None:
    """Claim an empty results directory before writing any part of this run."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if any(directory.iterdir()):
            raise ResultsDirectoryError("Results directory must be empty at startup")
        # mkdir is exclusive even when two processes pass the empty check together.
        (directory / "logs").mkdir()
    except OSError as exc:
        raise ResultsDirectoryError(f"Cannot reserve {directory}: {exc}") from exc


@dataclass(frozen=True)
class Variant:
    name: str
    episodes: int


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    dimension: str
    seed: int
    upstream_revision: str
    variants: tuple[Variant, ...]
    # True only for the CPU mock, which is not a benchmark task.
    mock: bool = False


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise EvaluationError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path: Path) -> dict[str, Any]:
    """Read one JSON object, rejecting duplicate keys and nonstandard numbers."""
    def reject_constant(value: str) -> None:
        raise EvaluationError(f"Non-finite JSON value: {value}")

    try:
        with path.open(encoding="utf-8") as source:
            value = json.load(
                source, object_pairs_hook=_unique_object,
                parse_constant=reject_constant,
            )
    except (OSError, ValueError) as exc:
        raise EvaluationError(f"Cannot read JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise EvaluationError(f"Expected a JSON object in {path}")
    return value


def _identical(left: Any, right: Any) -> bool:
    """Equal in value and in JSON type, so that 50.0 is not 50 and true is not 1."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_identical(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(map(_identical, left, right))
    return left == right


def load_spec(path: Path) -> TaskSpec:
    """Accept only the specification that the catalog defines for this task and seed.

    The catalog is the single definition of the native protocol. A file that
    differs from it in any field, value or type is refused.
    """
    value = read_json(path)
    mock = "mock" in value
    if mock and value["mock"] is not True:
        raise EvaluationError("A task specification is either a mock or has no mock field")
    seed = value.get("seed")
    if type(seed) is not int or seed < 0:
        raise EvaluationError("Task seed must be a nonnegative integer")
    try:
        if mock:
            # Only the mock may choose its episode count, within its own limits.
            variants = value.get("variants")
            single = isinstance(variants, list) and len(variants) == 1 and isinstance(variants[0], dict)
            expected = mock_task_spec(seed, variants[0].get("episodes") if single else None)
        else:
            expected = task_spec(value.get("task_id"), seed)
    except ValueError as exc:
        raise EvaluationError(str(exc)) from exc
    missing = object()
    different = sorted(
        key for key in value.keys() | expected.keys()
        if not _identical(value.get(key, missing), expected.get(key, missing))
    )
    if different:
        kind = "Mock task" if mock else "Task"
        raise EvaluationError(
            f"{kind} specification does not match the pinned native protocol in: {', '.join(different)}"
        )
    return TaskSpec(
        expected["task_id"], expected["dimension"], seed, expected["upstream_revision"],
        tuple(Variant(**variant) for variant in expected["variants"]), mock=mock,
    )


def _number(value: Any, field: str, upper: float = 1.0) -> float:
    if type(value) not in (int, float) or not 0 <= value <= upper or not math.isfinite(value):
        raise EvaluationError(f"{field} must be a finite number in [0, {upper:g}]")
    return float(value)


def validate_result(value: dict[str, Any], episodes: int) -> tuple[int, float]:
    """Validate native totals and unique evaluated layouts, returning raw totals.

    Upstream stores per-episode scores in [0, 1] but its aggregate score in
    [0, 100]. Harbor's score is normalized to [0, 1] only after validation.
    Native recovery can replace unstable layouts with later layout IDs, so
    IDs need not form a contiguous range even for a complete evaluation.
    """
    if type(value.get("eval_time")) is not int or value["eval_time"] != episodes:
        raise EvaluationError("Native result is incomplete or has an unexpected eval_time")
    details = value.get("details")
    if not isinstance(details, dict) or len(details) != episodes:
        raise EvaluationError("Native result must contain exactly one detail per episode")
    layouts: set[int] = set()
    successes, scores = 0, []
    for detail in details.values():
        if not isinstance(detail, dict):
            raise EvaluationError("Each episode detail must be an object")
        layout = detail.get("layout_id")
        if type(layout) is not int or layout < 0 or layout in layouts:
            raise EvaluationError("Native layout IDs must be unique nonnegative integers")
        layouts.add(layout)
        if type(detail.get("success")) is not bool:
            raise EvaluationError("Each episode success value must be boolean")
        score = _number(detail.get("score"), "Episode score")
        if detail["success"] and score != 1.0:
            raise EvaluationError("A successful native episode must have a score of 1")
        successes += detail["success"]
        scores.append(score)
    total_score = math.fsum(scores)
    expected_success = successes / episodes
    expected_score = total_score / episodes * 100
    if not math.isclose(
        _number(value.get("success_rate"), "success_rate"), expected_success,
        rel_tol=1e-9, abs_tol=1e-9,
    ):
        raise EvaluationError("Native success_rate does not match episode details")
    if not math.isclose(
        _number(value.get("score"), "Aggregate score", upper=100), expected_score,
        rel_tol=1e-9, abs_tol=1e-9,
    ):
        raise EvaluationError("Native score does not match episode details")
    return successes, total_score


def write_json(path: Path, value: Any) -> None:
    """Write JSON atomically so that readers never see a partial file."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)
