"""Behaviour of the task verifier (task-template/tests/grade.py)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from latchbio.oracle import TEMPLATE_TESTS_DIR, load_grade_module

from conftest import EVALS

QC = EVALS["evals/qc/QC01_cell_counts.json"]
MULTI = EVALS["evals/multi/eval.json"]
GOOD_QC = {"n_cells": 1500, "stats": {"median_genes": 68.5}}


def run_verifier(tmp_path: Path, eval_config: dict, files: dict[str, str]) -> tuple[int, str, dict]:
    """Run grade.py as the container would and return (exit code, reward, report)."""
    workspace = tmp_path / "workspace"
    output = tmp_path / "logs"
    workspace.mkdir(parents=True)
    for name, content in files.items():
        (workspace / name).write_text(content)
    config = tmp_path / "eval_config.json"
    config.write_text(json.dumps(eval_config))
    proc = subprocess.run(
        [
            sys.executable,
            str(TEMPLATE_TESTS_DIR / "grade.py"),
            "--config", str(config),
            "--workspace", str(workspace),
            "--output-dir", str(output),
        ],
        capture_output=True,
        text=True,
    )
    reward = (output / "reward.txt").read_text().strip()
    report = json.loads((output / "grading.json").read_text())
    return proc.returncode, reward, report


def qc_config() -> dict:
    return {"id": QC["id"], "grader": QC["grader"]}


def test_correct_answer_gets_reward_1(tmp_path):
    code, reward, report = run_verifier(
        tmp_path, qc_config(), {"eval_answer.json": json.dumps(GOOD_QC)}
    )
    assert (code, reward) == (0, "1")
    assert report["passed"] is True and report["graded"] is True
    assert report["grader_results"][0]["type"] == "numeric_tolerance"


def test_answer_within_tolerance_passes_and_outside_fails(tmp_path):
    inside = {"n_cells": 1549, "stats": {"median_genes": 69.4}}
    outside = {"n_cells": 1551, "stats": {"median_genes": 68.5}}
    assert run_verifier(tmp_path / "a", qc_config(), {"eval_answer.json": json.dumps(inside)})[1] == "1"
    assert run_verifier(tmp_path / "b", qc_config(), {"eval_answer.json": json.dumps(outside)})[1] == "0"


def test_asymmetric_tolerance_is_respected(tmp_path):
    # stats.median_genes accepts [66.5, 69.5]: 2 below, 1 above the ground truth.
    low_ok = {"n_cells": 1500, "stats": {"median_genes": 66.6}}
    high_bad = {"n_cells": 1500, "stats": {"median_genes": 69.6}}
    assert run_verifier(tmp_path / "a", qc_config(), {"eval_answer.json": json.dumps(low_ok)})[1] == "1"
    assert run_verifier(tmp_path / "b", qc_config(), {"eval_answer.json": json.dumps(high_bad)})[1] == "0"


@pytest.mark.parametrize(
    "files, expected_error",
    [
        ({}, "no answer file found"),
        ({"eval_answer.json": "{not json"}, "failed to parse"),
        ({"eval_answer.json": "null"}, "JSON null"),
    ],
)
def test_missing_or_unparseable_answers_are_not_passed(tmp_path, files, expected_error):
    code, reward, report = run_verifier(tmp_path, qc_config(), files)
    assert (code, reward) == (0, "0")
    assert report["passed"] is False and report["graded"] is False
    assert expected_error in report["error"]


@pytest.mark.parametrize("answer", ["[1, 2, 3]", '"1500"', "1500", "{}", '{"n_cells": "many"}'])
def test_malformed_answers_fail_without_crashing_the_verifier(tmp_path, answer):
    code, reward, report = run_verifier(tmp_path, qc_config(), {"eval_answer.json": answer})
    assert (code, reward) == (0, "0")
    assert report["passed"] is False


def test_missing_graded_field_fails(tmp_path):
    answer = {"n_cells": 1500}
    assert run_verifier(tmp_path, qc_config(), {"eval_answer.json": json.dumps(answer)})[1] == "0"


def test_analysis_output_json_takes_precedence_like_upstream(tmp_path):
    # latch_eval_tools.harness.utils.ANSWER_FILENAMES checks analysis_output.json first.
    files = {
        "analysis_output.json": json.dumps({"n_cells": 1, "stats": {"median_genes": 1}}),
        "eval_answer.json": json.dumps(GOOD_QC),
    }
    code, reward, report = run_verifier(tmp_path, qc_config(), files)
    assert reward == "0"
    assert report["answer_file"].endswith("analysis_output.json")


def test_graders_list_passes_only_when_every_grader_passes(tmp_path):
    config = {"id": MULTI["id"], "graders": MULTI["graders"]}
    both = {"genes": ["BCAT2", "PPM1K"], "beta": 0.25}
    one = {"genes": ["BCAT2", "PPM1K"], "beta": 0.9}
    code, reward, report = run_verifier(tmp_path / "a", config, {"eval_answer.json": json.dumps(both)})
    assert reward == "1" and len(report["grader_results"]) == 2
    code, reward, report = run_verifier(tmp_path / "b", config, {"eval_answer.json": json.dumps(one)})
    assert reward == "0"
    assert [entry["passed"] for entry in report["grader_results"]] == [True, False]
    assert report["score"] == pytest.approx(0.5)


def test_eval_config_must_define_exactly_one_grader_field():
    grade = load_grade_module()
    with pytest.raises(ValueError):
        grade.grade_answer({"id": "x"}, {"a": 1})
    with pytest.raises(ValueError):
        grade.grade_answer({"id": "x", "grader": QC["grader"], "graders": MULTI["graders"]}, {"a": 1})


def test_unknown_grader_type_is_a_verifier_error_not_an_agent_failure():
    grade = load_grade_module()
    with pytest.raises(ValueError, match="unknown grader type"):
        grade.grade_answer({"id": "x", "grader": {"type": "nope", "config": {}}}, {"a": 1})


def test_answer_file_lookup_matches_upstream_order():
    assert load_grade_module().ANSWER_FILENAMES == ("analysis_output.json", "eval_answer.json")
