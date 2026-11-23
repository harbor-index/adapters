"""Adapter tests. Unit tests run offline; set DISTDEBUG_SOURCE to an upstream
checkout (commit c3e929b) to also run the end-to-end generation tests:

    DISTDEBUG_SOURCE=/path/to/distdebug-bench uv run pytest tests
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from distdebug_bench.adapter import (
    CLI_FINISH,
    DistDebugBenchAdapter,
    SNAPSHOTS_FILE,
    Bug,
    _render,
)

SOURCE = os.environ.get("DISTDEBUG_SOURCE")
needs_source = pytest.mark.skipif(not SOURCE, reason="set DISTDEBUG_SOURCE to an upstream checkout")


def _bug(artifacts: list[Path] | None = None) -> Bug:
    return Bug(
        bug_id="zookeeper-1",
        system="zookeeper",
        number="1",
        report={"title": "T", "description": "  D {x}  \n", "branch": "release-1"},
        blocks=[{"file": "a/B.java", "function": "B.c", "start_line": "10", "end_line": 12}],
        bug_dir=Path("."),
        artifact_files=artifacts or [],
    )


def test_render_only_known_keys():
    assert _render("{a} {b} {}", {"a": "{b}"}) == "{b} {b} {}"


def test_build_task_text_matches_upstream_format(tmp_path):
    assert DistDebugBenchAdapter.build_task_text(_bug()) == "Title: T\n\nD {x}"
    log = tmp_path / "node1.log"
    log.write_text("x")
    text = DistDebugBenchAdapter.build_task_text(_bug([log]))
    assert text.endswith("Available artifact files:\n- node1.log\n")
    assert "`./.artifacts/`" in text


def test_oracle_diagnosis_is_schema_valid_and_int_lines():
    diag = DistDebugBenchAdapter.oracle_diagnosis(_bug())
    block = diag["buggy_blocks"][0]
    assert set(block) == {"file", "function", "start_line", "end_line", "bug_contribution", "patch"}
    assert block["start_line"] == 10 and isinstance(block["start_line"], int)
    assert set(diag) == {"buggy_blocks", "bug_cause_description"}


def test_snapshots_are_full_shas():
    bugs = json.loads(SNAPSHOTS_FILE.read_text())["bugs"]
    assert len(bugs) == 120
    assert all(len(b["commit"]) == 40 and int(b["commit"], 16) >= 0 for b in bugs.values())


@needs_source
def test_generate_lite(tmp_path):
    out = tmp_path / "out"
    written = DistDebugBenchAdapter(out, source_dir=Path(SOURCE)).run()
    assert len(written) == 34
    names = set()
    for task in written:
        cfg = tomllib.loads((task / "task.toml").read_text())
        names.add(cfg["task"]["name"])
        assert cfg["task"]["name"] == f"orderlab/distdebug-bench__{task.name}"
        assert cfg["verifier"]["environment_mode"] == "separate"
        assert cfg["artifacts"] == ["/workspace/diagnosis.json"]
        assert cfg["agent"]["network_mode"] == "allowlist"
        assert CLI_FINISH in (task / "instruction.md").read_text()
        # Labels and fix links live only in the verifier's build context.
        for path in (task / "environment").rglob("*"):
            assert path.name not in ("blocks.json", "report.json")
        assert "fix_link" not in (task / "instruction.md").read_text()
        for name in ("Dockerfile", "test.sh", "score_diagnosis.py", "bug_id.txt", "upstream_manifest.json"):
            assert (task / "tests" / name).is_file()
    assert len(names) == 34


@needs_source
def test_oracle_and_nop_scores_with_generated_verifier(tmp_path):
    """Run each task's own solve.sh output through its own tests/score_diagnosis.py."""
    out = tmp_path / "out"
    DistDebugBenchAdapter(out, source_dir=Path(SOURCE), task_ids=["zookeeper-2845", "ray-25699"]).run()
    for task in sorted(out.iterdir()):
        solve = (task / "solution" / "solve.sh").read_text()
        oracle = solve.split("<<'DISTDEBUG_ORACLE_EOF'\n", 1)[1].split("\nDISTDEBUG_ORACLE_EOF", 1)[0]
        for name, content in (("oracle", oracle), ("nop", None)):
            diag = tmp_path / f"{task.name}-{name}.json"
            if content is not None:
                diag.write_text(content)
            logs = tmp_path / f"{task.name}-{name}-logs"
            subprocess.run(
                [sys.executable, str(task / "tests" / "score_diagnosis.py"), "--benchmark", str(task / "tests"),
                 "--bug", task.name, "--diagnosis", str(diag), "--out-dir", str(logs)],
                check=True, capture_output=True,
            )
            reward = json.loads((logs / "reward.json").read_text())
            assert all(isinstance(v, (int, float)) for v in reward.values())
            if name == "oracle":
                assert reward["reward"] == 1.0 and reward["valid_diagnosis"] == 1
            else:
                assert reward["reward"] == 0.0 and reward["valid_diagnosis"] == 0
