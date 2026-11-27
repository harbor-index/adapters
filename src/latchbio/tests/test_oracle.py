"""Oracle answers are derived from grader configs and must pass those graders."""

from __future__ import annotations

import copy

import pytest

from latchbio.oracle import OracleError, load_grade_module, synthesize_oracle_answer

from conftest import EVALS

QC = EVALS["evals/qc/QC01_cell_counts.json"]
MARKERS = EVALS["evals/markers/eval.json"]
MULTI = EVALS["evals/multi/eval.json"]


def _config(raw: dict) -> dict:
    return {key: raw[key] for key in ("id", "grader", "graders") if key in raw}


def test_numeric_tolerance_oracle_uses_ground_truth_and_nested_fields():
    answer = synthesize_oracle_answer(_config(QC))
    assert answer == {"n_cells": 1500, "stats": {"median_genes": 68.5}}


def test_all_of_oracle_merges_every_child():
    answer = synthesize_oracle_answer(_config(MARKERS))
    assert answer == {
        "top_marker_genes": ["CD3E", "CD3D", "CD2", "IL7R"],
        "lineages": ["T cell", "B cell"],
        "answer": "B",
    }


def test_top_level_graders_list_oracle_covers_all_graders():
    answer = synthesize_oracle_answer(_config(MULTI))
    assert answer == {"genes": ["BCAT2", "PPM1K"], "beta": 0.25}


def test_per_celltype_markers_are_returned_as_an_object():
    config = {
        "id": "per_celltype",
        "grader": {
            "type": "marker_gene_precision_recall",
            "config": {
                "answer_field": "markers",
                "canonical_markers": {"T": ["CD3E", "CD3D"], "B": ["CD79A", "MS4A1"]},
                "scoring": {"pass_thresholds": {"min_recall_per_celltype": 0.5}},
            },
        },
    }
    assert synthesize_oracle_answer(config)["markers"] == {
        "T": ["CD3E", "CD3D"],
        "B": ["CD79A", "MS4A1"],
    }


def test_oracle_answer_is_a_copy_not_a_view_of_the_config():
    config = _config(MARKERS)
    answer = synthesize_oracle_answer(config)
    answer["lineages"].append("mutated")
    assert config["grader"]["config"]["children"][1]["config"]["ground_truth_labels"] == [
        "T cell",
        "B cell",
    ]


def test_unsupported_grader_type_is_rejected():
    config = {"id": "x", "grader": {"type": "spatial_adjacency", "config": {}}}
    with pytest.raises(OracleError, match="No oracle synthesis"):
        synthesize_oracle_answer(config)


def test_conflicting_ground_truth_between_graders_is_rejected():
    config = copy.deepcopy(_config(MULTI))
    config["graders"].append(
        {
            "type": "numeric_tolerance",
            "config": {
                "ground_truth": {"beta": 0.9},
                "tolerances": {"beta": {"type": "absolute", "value": 0.05}},
            },
        }
    )
    with pytest.raises(OracleError, match="disagree"):
        synthesize_oracle_answer(config)


def test_oracle_that_cannot_pass_its_grader_is_rejected():
    # A "min" tolerance above the ground truth: reproducing the ground truth fails.
    config = {
        "id": "unsatisfiable",
        "grader": {
            "type": "numeric_tolerance",
            "config": {
                "ground_truth": {"x": 1},
                "tolerances": {"x": {"type": "min", "value": 5}},
            },
        },
    }
    with pytest.raises(OracleError, match="does not pass"):
        synthesize_oracle_answer(config)


def test_generation_and_verifier_share_one_grading_module():
    assert load_grade_module() is load_grade_module()
    assert "numeric_tolerance" in load_grade_module().GRADER_REGISTRY
