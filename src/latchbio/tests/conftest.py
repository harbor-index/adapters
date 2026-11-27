"""Shared fixtures: a tiny local "upstream" repository with synthetic evals.

The real upstream eval files carry LatchBio's benchmark canary and must not be
copied into this repository, so the tests build look-alike evals instead. They
exercise every grader type used by the adapted evaluations.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from latchbio.sources import BenchmarkSource

EVALS = {
    "evals/qc/QC01_cell_counts.json": {
        "id": "QC01_cell_counts",
        "task": "Count the cells.\n\n<EVAL_ANSWER>\n{\"n_cells\": <int>}\n</EVAL_ANSWER>",
        "data_node": "latch://1001.node",
        "notes": "Ground truth derived by counting rows.",
        "metadata": {"task": "qc", "kit": "xenium", "time_horizon": "small", "eval_type": "scientific"},
        "grader": {
            "type": "numeric_tolerance",
            "config": {
                "ground_truth": {"n_cells": 1500, "stats.median_genes": 68.5},
                "tolerances": {
                    "n_cells": {"type": "absolute", "value": 50},
                    "stats.median_genes": {"type": "absolute", "lower": 2, "upper": 1},
                },
            },
        },
    },
    "evals/markers/eval.json": {
        "id": "marker_panel",
        "task": "Report marker genes and the lineages present.",
        "data_node": ["latch://1002.node", "latch://1003.node"],
        "metadata": {"task": "cell_typing", "kit": "none"},
        "grader": {
            "type": "all_of",
            "config": {
                "pass_rule": "all",
                "children": [
                    {
                        "type": "marker_gene_precision_recall",
                        "config": {
                            "answer_field": "top_marker_genes",
                            "canonical_markers": ["CD3E", "CD3D", "CD2", "IL7R"],
                            "scoring": {"pass_thresholds": {"precision_at_k": 0.5, "recall_at_k": 0.5}},
                        },
                    },
                    {
                        "type": "label_set_jaccard",
                        "config": {
                            "answer_field": "lineages",
                            "ground_truth_labels": ["T cell", "B cell"],
                            "scoring": {"pass_threshold": 0.9},
                        },
                    },
                    {"type": "multiple_choice", "config": {"correct_answer": "B"}},
                ],
            },
        },
    },
    "evals/multi/eval.json": {
        "id": "Multi_Grader",
        "task": "Report a lead gene set and an effect size.",
        "data_node": ["latch://1004.node"],
        "metadata": {"task": "statistics"},
        "graders": [
            {
                "type": "label_set_jaccard",
                "config": {
                    "answer_field": "genes",
                    "ground_truth_labels": ["BCAT2", "PPM1K"],
                    "scoring": {"pass_threshold": 1.0},
                },
            },
            {
                "type": "numeric_tolerance",
                "config": {
                    "ground_truth": {"beta": 0.25},
                    "tolerances": {"beta": {"type": "absolute", "value": 0.05}},
                },
            },
        ],
    },
    # Bookkeeping files upstream keeps next to the evals; never tasks.
    "evals/manifest.json": [{"eval_id": "QC01_cell_counts"}],
    "evals/markers/vocabulary.json": {"lineages": ["T cell", "B cell", "NK cell"]},
    # Outside the eval directory; must be ignored.
    "trajectories/QC01_cell_counts/result.json": {"task": "QC01_cell_counts"},
}


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def upstream_root(tmp_path: Path) -> Path:
    """A directory holding one committed fake benchmark repository (``fakebench``)."""
    repo = tmp_path / "upstream" / "fakebench"
    for rel, content in EVALS.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(content, indent=2))
    _git("init", "--quiet", cwd=repo)
    _git("add", "-A", cwd=repo)
    _git(
        "-c", "user.name=test", "-c", "user.email=test@example.com",
        "commit", "--quiet", "-m", "fake benchmark",
        cwd=repo,
    )
    return repo.parent


@pytest.fixture
def fake_source(upstream_root: Path) -> BenchmarkSource:
    repo = upstream_root / "fakebench"
    return BenchmarkSource(
        key="fakebench",
        display_name="FakeBench",
        repo=f"https://example.invalid/latchbio/{repo.name}",
        commit=_git("rev-parse", "HEAD", cwd=repo),
        evals_dir="evals",
        authors=({"name": "Ada Lovelace"}, {"name": "Grace Hopper", "email": "grace@example.com"}),
    )


@pytest.fixture
def patch_sources(monkeypatch: pytest.MonkeyPatch, fake_source: BenchmarkSource) -> BenchmarkSource:
    """Point the adapter at the fake benchmark instead of the pinned LatchBio repos."""
    import latchbio.adapter as adapter_module

    monkeypatch.setattr(adapter_module, "BENCHMARKS", (fake_source,))
    monkeypatch.setattr(adapter_module, "BENCHMARKS_BY_KEY", {fake_source.key: fake_source})
    return fake_source
