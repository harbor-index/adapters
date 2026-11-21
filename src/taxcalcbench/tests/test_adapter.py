"""Equivalence tests against a pinned TaxCalcBench checkout.

Run from src/taxcalcbench/:
    TAXCALCBENCH_ROOT=/path/to/tax-calc-bench uv run --with pytest pytest tests

Checks, for every generated task:
- instruction.md starts with the exact prompt upstream builds for that case;
- the vendored scorer (tests/score.py) returns the same strict / lenient /
  by-line results as upstream ``TaxReturnEvaluator`` on the oracle answer, an
  empty answer and an answer with one line off by $1;
- the oracle is strictly correct and the empty answer is not.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from taxcalcbench.adapter import TY25, TaxCalcBenchAdapter

ROOT = os.environ.get("TAXCALCBENCH_ROOT")
pytestmark = pytest.mark.skipif(not ROOT, reason="set TAXCALCBENCH_ROOT to an upstream checkout")


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    out = tmp_path_factory.mktemp("tasks")
    adapter = TaxCalcBenchAdapter(output_dir=out, benchmark_root=Path(ROOT))
    adapter.run()
    return adapter, out


@pytest.fixture(scope="module")
def upstream_evaluator():
    sys.path.insert(0, ROOT)
    try:
        from tax_calc_bench.tax_return_evaluator import TaxReturnEvaluator
    finally:
        sys.path.remove(ROOT)
    return TaxReturnEvaluator()


def _load_scorer(task_dir: Path):
    spec = importlib.util.spec_from_file_location(f"score_{task_dir.name}", task_dir / "tests/score.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _off_by_one(oracle: str) -> str:
    lines = oracle.splitlines()
    for i, line in enumerate(lines):
        if " | " in line and not line.rstrip().endswith("| 0"):
            head, amount = line.rsplit("| ", 1)
            lines[i] = f"{head}| {repr(float(amount) + 1)}"
            break
    return "\n".join(lines) + "\n"


def test_all_cases_generated(generated):
    adapter, out = generated
    cases = adapter.discover()
    assert len(cases) == 101
    assert sorted(p.name for p in out.iterdir()) == sorted(c.task_id for c in cases)


def test_instruction_starts_with_upstream_prompt(generated):
    adapter, out = generated
    for case in adapter.discover():
        prompt = adapter._upstream_prompt(case)
        instruction = (out / case.task_id / "instruction.md").read_text()
        assert instruction.startswith(prompt), case.task_id


def test_scorer_matches_upstream(generated, upstream_evaluator):
    adapter, out = generated
    for case in adapter.discover():
        task_dir = out / case.task_id
        scorer = _load_scorer(task_dir)
        spec = json.loads((task_dir / "tests/scored_fields.json").read_text())
        expected_xml = (task_dir / "tests/expected.xml").read_text()
        oracle = (task_dir / "solution/oracle_return.txt").read_text()
        jurisdiction = case.source_id.split("-")[1] if case.tax_year == TY25 else None

        answers = [("oracle", oracle), ("empty", ""), ("off_by_one", _off_by_one(oracle)), ("duplicate", oracle + oracle)]
        for name, answer in answers:
            mine = scorer.evaluate(answer, expected_xml, spec["tax_year"], spec["fields"])
            theirs = upstream_evaluator.evaluate(
                answer, expected_xml, tax_year=case.tax_year, jurisdiction=jurisdiction
            )
            assert mine["strictly_correct_return"] == theirs.strictly_correct_return, (case.task_id, name)
            assert mine["lenient_correct_return"] == theirs.lenient_correct_return, (case.task_id, name)
            assert mine["correct_by_line_score"] == pytest.approx(theirs.correct_by_line_score), (case.task_id, name)
            assert mine["lenient_correct_by_line_score"] == pytest.approx(
                theirs.lenient_correct_by_line_score
            ), (case.task_id, name)
            if name == "oracle":
                assert mine["strictly_correct_return"], case.task_id
            elif name == "duplicate":
                # Upstream TY25 parsing ignores a label that appears more than once;
                # TY24 parsing takes the first occurrence. Matching upstream is what counts.
                if case.tax_year == TY25:
                    assert not mine["strictly_correct_return"], (case.task_id, name)
            else:
                assert not mine["strictly_correct_return"], (case.task_id, name)
