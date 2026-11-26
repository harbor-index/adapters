"""Access to the unmodified upstream graders that ship inside every generated task.

``task-template/tests/grading.py`` and ``task-template/tests/grade_compliance_csv.py`` are
byte-for-byte copies of the upstream files. The adapter imports the very same files to
check, at generation time, that each oracle trace is graded as compliant.
"""

from __future__ import annotations

import contextlib
import functools
import importlib
import io
import sys
from pathlib import Path
from types import ModuleType

import pandas as pd

TEMPLATE_DIR = Path(__file__).resolve().parent / "task-template"
TEMPLATE_TESTS_DIR = TEMPLATE_DIR / "tests"


@functools.lru_cache(maxsize=1)
def load_upstream_graders() -> tuple[ModuleType, ModuleType]:
    """Import ``(grading, grade_compliance_csv)`` from the task template."""
    tests_dir = str(TEMPLATE_TESTS_DIR)
    sys.path.insert(0, tests_dir)
    try:
        # grade_compliance_csv prints a notice when the optional `openai` package is
        # missing; that is irrelevant for the rule-based graders used here.
        with contextlib.redirect_stdout(io.StringIO()):
            grading = importlib.import_module("grading")
            grade_compliance_csv = importlib.import_module("grade_compliance_csv")
    finally:
        sys.path.remove(tests_dir)
    return grading, grade_compliance_csv


def grade_rule_based(mode: str, prompt: str, reasoning: str) -> int:
    """Grade one trace with upstream ``grade_single_row`` (rule-based modes only)."""
    if mode == "ignore_question":
        raise ValueError("ignore_question is graded by an LLM judge, not by rules")
    _, grade_compliance_csv = load_upstream_graders()
    row = pd.Series({"mode": mode, "reasoning": reasoning, "question": prompt})
    with contextlib.redirect_stdout(io.StringIO()):
        return int(grade_compliance_csv.grade_single_row(row))
