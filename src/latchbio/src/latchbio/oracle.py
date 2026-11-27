"""Derive oracle answers from upstream grader configurations.

LatchBio does not publish reference analysis code. Each grader config does,
however, embed the ground truth an answer is compared against, so the oracle
answer for an evaluation is the JSON object that reproduces that ground truth
in every graded field.

The synthesis below covers the grader types used by the adapted evaluations.
Every synthesised answer is then graded with the upstream graders, and
task generation fails if the answer does not pass: an unsupported grader type
or an unsatisfiable config surfaces immediately instead of producing a task
whose oracle silently scores 0.
"""

from __future__ import annotations

import copy
import importlib.util
import sys
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any

TEMPLATE_TESTS_DIR = Path(__file__).parent / "task-template" / "tests"


class OracleError(ValueError):
    """Raised when no passing oracle answer can be derived for an eval."""


@lru_cache(maxsize=1)
def load_grade_module() -> ModuleType:
    """Import ``task-template/tests/grade.py``, the verifier shipped in every task.

    Generation, the test suite and the container verifier all grade through this
    one module so they cannot drift apart.
    """
    path = TEMPLATE_TESTS_DIR / "grade.py"
    spec = importlib.util.spec_from_file_location("latchbio_task_grade", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load verifier module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _set_field(answer: dict[str, Any], key: str, value: Any) -> None:
    """Set ``key`` on ``answer``, mirroring upstream's ``get_nested_value``.

    A key without a dot is a plain top-level field. A dotted key addresses a
    nested object, one level per segment.
    """
    if "." not in key:
        _assign(answer, key, value)
        return
    *parents, leaf = key.split(".")
    current = answer
    for part in parents:
        existing = current.setdefault(part, {})
        if not isinstance(existing, dict):
            raise OracleError(f"Field {key!r} conflicts with non-object value at {part!r}")
        current = existing
    _assign(current, leaf, value)


def _assign(target: dict[str, Any], key: str, value: Any) -> None:
    if key in target and target[key] != value:
        raise OracleError(
            f"Graders disagree on the reference value of {key!r}: "
            f"{target[key]!r} vs {value!r}"
        )
    target[key] = copy.deepcopy(value)


def _apply_grader(answer: dict[str, Any], spec: dict[str, Any]) -> None:
    grader_type = spec.get("type")
    config = spec.get("config") or {}

    if grader_type == "numeric_tolerance":
        for key, value in config["ground_truth"].items():
            _set_field(answer, key, value)
    elif grader_type in ("label_set_jaccard", "jaccard_label_set"):
        field = config.get("answer_field", "cell_types_predicted")
        _assign(answer, field, list(config["ground_truth_labels"]))
    elif grader_type == "marker_gene_precision_recall":
        field = config.get("answer_field", "top_marker_genes")
        markers = config.get("canonical_markers", config.get("ground_truth_labels"))
        if isinstance(markers, dict) and field in markers:
            markers = markers[field]
        _assign(answer, field, markers)
    elif grader_type == "multiple_choice":
        if "correct_answer" in config:
            _assign(answer, "answer", config["correct_answer"])
        else:
            _assign(answer, "answer", config["correct_answers"][0])
    elif grader_type in ("all_of", "composite"):
        for child in config.get("children") or []:
            _apply_grader(answer, child)
    else:
        raise OracleError(
            f"No oracle synthesis for grader type {grader_type!r}; extend "
            "latchbio.oracle before adapting evals that use it."
        )


def synthesize_oracle_answer(eval_config: dict[str, Any]) -> dict[str, Any]:
    """Build the reference answer for an eval and prove that it passes.

    ``eval_config`` needs the eval's ``id`` plus its ``grader`` or ``graders``.
    """
    specs = (
        [eval_config["grader"]]
        if eval_config.get("grader") is not None
        else list(eval_config.get("graders") or [])
    )
    if not specs:
        raise OracleError(f"Eval {eval_config.get('id')!r} defines no grader")

    answer: dict[str, Any] = {}
    for spec in specs:
        _apply_grader(answer, spec)

    report = load_grade_module().grade_answer(eval_config, answer)
    if not report["passed"]:
        reasons = "\n".join(
            str(entry.get("reasoning", "")) for entry in report["grader_results"]
        )
        raise OracleError(
            f"Synthesised oracle answer for {eval_config.get('id')!r} does not pass "
            f"its own grader:\n{reasons}"
        )
    return answer
