"""The parity agent builds the same request as upstream, for every case.

No API call is made. Run from src/taxcalcbench/ with the ``parity`` extra:
    TAXCALCBENCH_ROOT=/path/to/tax-calc-bench uv run --extra parity --with pytest pytest tests
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

ROOT = os.environ.get("TAXCALCBENCH_ROOT")
pytestmark = pytest.mark.skipif(not ROOT, reason="set TAXCALCBENCH_ROOT to an upstream checkout")

MODEL = "openai/gpt-6.2-sol"


@pytest.fixture(scope="module")
def tasks(tmp_path_factory):
    from taxcalcbench.adapter import TaxCalcBenchAdapter

    out = tmp_path_factory.mktemp("tasks")
    adapter = TaxCalcBenchAdapter(output_dir=out, benchmark_root=Path(ROOT))
    adapter.run()
    return adapter, out


@pytest.fixture(scope="module")
def upstream():
    from taxcalcbench.parity_agent import _load_upstream

    up = _load_upstream()
    from tax_calc_bench.config import TAX_YEAR
    from tax_calc_bench.ty24_prompt import TAX_RETURN_GENERATION_PROMPT

    up["TY24"] = (TAX_YEAR, TAX_RETURN_GENERATION_PROMPT)
    return up


@pytest.fixture(scope="module")
def agent(tmp_path_factory):
    from taxcalcbench.parity_agent import TaxCalcBenchDirectAgent

    return TaxCalcBenchDirectAgent(logs_dir=tmp_path_factory.mktemp("logs"), model_name=MODEL)


def _upstream_request(gen, case, monkeypatch) -> dict:
    """Run upstream's real per-test path and capture the request it would send."""
    captured: list[dict] = []

    class _Stop(BaseException):
        pass

    def fake_responses(**kwargs):
        captured.append(kwargs)
        raise _Stop()

    monkeypatch.setattr(gen, "responses", fake_responses)
    try:
        gen.run_tax_return_test(MODEL, case.source_id, "high", None, case.tax_year)
    except _Stop:
        pass
    assert len(captured) == 1, case.task_id
    return captured[0]


def test_request_matches_upstream(tasks, upstream, agent, monkeypatch):
    """The agent's request equals the one upstream's own code path builds."""
    from taxcalcbench.parity_agent import upstream_prompt_from_instruction

    adapter, out = tasks
    gen = upstream["gen"]
    monkeypatch.chdir(ROOT)  # upstream reads test data relative to cwd

    for case in adapter.discover():
        task_dir = out / case.task_id
        prompt = upstream_prompt_from_instruction((task_dir / "instruction.md").read_text())
        mine = agent.build_request(prompt, task_dir / "environment" / "input")
        theirs = _upstream_request(gen, case, monkeypatch)
        assert mine == theirs, case.task_id


def test_prompt_needs_harbor_notes():
    from taxcalcbench.parity_agent import upstream_prompt_from_instruction

    with pytest.raises(ValueError):
        upstream_prompt_from_instruction("no notes here")
