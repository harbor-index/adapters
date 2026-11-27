"""Statistics and result loading of parity/summarize.py."""

from __future__ import annotations

import importlib.util
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "parity" / "summarize.py"


@pytest.fixture(scope="module")
def summarize():
    spec = importlib.util.spec_from_file_location("latchbio_parity_summarize", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sample_sem_matches_the_adapter_guide_formula(summarize):
    # docs/adapters.mdx example: [44.0, 45.5, 46.1] -> 45.2 +/- 0.6245
    runs = [44.0, 45.5, 46.1]
    assert summarize.mean(runs) == pytest.approx(45.2)
    assert summarize.sample_sem(runs) == pytest.approx(0.6245, abs=5e-5)
    # sqrt( sum((x - mean)^2) / (n (n - 1)) ), i.e. sample std / sqrt(n)
    m = sum(runs) / 3
    assert summarize.sample_sem(runs) == pytest.approx(
        math.sqrt(sum((x - m) ** 2 for x in runs) / (3 * 2))
    )


def test_sample_sem_is_undefined_for_a_single_run(summarize):
    with pytest.raises(ValueError):
        summarize.sample_sem([50.0])


@pytest.mark.parametrize(
    "a, b, expected",
    [
        ([40.0, 45.0], [44.0, 50.0], True),
        ([40.0, 45.0], [45.0, 50.0], True),   # touching ranges overlap
        ([40.0, 44.9], [45.0, 50.0], False),  # close means are not enough
        ([50.0, 60.0], [52.0, 53.0], True),   # containment
    ],
)
def test_range_overlap_rule(summarize, a, b, expected):
    assert summarize.ranges_overlap(a, b) is expected
    assert summarize.ranges_overlap(b, a) is expected


def _original_run(root: Path, results: dict[str, bool], errors: tuple[str, ...] = ()) -> Path:
    for task, passed in results.items():
        directory = root / task
        directory.mkdir(parents=True)
        record = {"task_id": task, "passed": passed}
        if task in errors:
            record["harness_error"] = "RuntimeError: docker died"
        (directory / "result.json").write_text(json.dumps(record))
    return root


def _harbor_job(root: Path, results: dict[str, bool], exceptions: dict[str, str] | None = None) -> Path:
    for index, (task, passed) in enumerate(results.items()):
        directory = root / f"trial-{index}"
        directory.mkdir(parents=True)
        record = {
            "task_name": f"latchbio/{task}",
            "verifier_result": {"rewards": {"reward": 1.0 if passed else 0.0}},
            "exception_info": None,
        }
        if exceptions and task in exceptions:
            record["exception_info"] = {"exception_type": exceptions[task]}
            if exceptions[task] != "AgentTimeoutError":
                record["verifier_result"] = None
        (directory / "result.json").write_text(json.dumps(record))
    return root


def test_loaders_report_accuracy_and_infrastructure_failures(summarize, tmp_path):
    original, errors = summarize.load_original(
        _original_run(tmp_path / "o", {"a": True, "b": False, "c": False}, errors=("c",))
    )
    assert summarize.accuracy(original) == pytest.approx(33.33)
    assert errors == ["c (harness error: RuntimeError: docker died)"]

    harbor, errors = summarize.load_harbor(
        _harbor_job(
            tmp_path / "h",
            {"a": True, "b": True, "c": False, "d": False},
            exceptions={"c": "AgentTimeoutError", "d": "EnvironmentStartTimeoutError"},
        )
    )
    assert harbor == {"a": True, "b": True, "c": False, "d": False}
    assert summarize.accuracy(harbor) == 50.0
    # A timed-out agent is a scored outcome; a failed environment is not.
    assert errors == ["d (EnvironmentStartTimeoutError)"]


def test_cli_reports_match_and_exits_nonzero_without_overlap(tmp_path):
    tasks = [f"t{i}" for i in range(10)]

    def run(original_passes, harbor_passes):
        root = tmp_path / f"case-{len(list(tmp_path.iterdir()))}"
        originals = [
            _original_run(root / f"o{i}", {t: j < n for j, t in enumerate(tasks)})
            for i, n in enumerate(original_passes)
        ]
        harbors = [
            _harbor_job(root / f"h{i}", {t: j < n for j, t in enumerate(tasks)})
            for i, n in enumerate(harbor_passes)
        ]
        out = root / "summary.json"
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--original", *map(str, originals),
             "--harbor", *map(str, harbors), "--output", str(out)],
            capture_output=True,
            text=True,
        )
        return proc, json.loads(out.read_text())

    proc, summary = run([4, 5, 6], [5, 5, 7])
    assert proc.returncode == 0, proc.stderr
    assert summary["ranges_overlap"] is True
    assert summary["metric"]["original_runs"] == [40.0, 50.0, 60.0]
    assert summary["metric"]["original"] == "50.00 ± 5.77"
    assert summary["metric"]["harbor"] == "56.67 ± 6.67"
    assert summary["per_task_passes"]["t4"] == {"original": 2, "harbor": 3}

    proc, summary = run([2, 3], [8, 9])
    assert proc.returncode == 1
    assert summary["ranges_overlap"] is False


SUCCESS = {"type": "result", "subtype": "success", "is_error": False, "result": "done"}
API_ERROR = {"type": "result", "subtype": "success", "is_error": True, "result": "API Error: 429 rate_limit"}


def test_agent_stream_classification(summarize):
    init = {"type": "system", "subtype": "init"}
    assert summarize.agent_stream_problem([init, SUCCESS], timed_out=False) is None
    assert "429" in summarize.agent_stream_problem([init, API_ERROR], timed_out=False)
    assert "no final result" in summarize.agent_stream_problem([init], timed_out=False)
    # A run that hit the time limit is a scored outcome even without a result event.
    assert summarize.agent_stream_problem([init], timed_out=True) is None
    # Without a transcript only harness-level signals apply.
    assert summarize.agent_stream_problem(None, timed_out=False) is None


def test_both_sides_flag_an_agent_api_error_the_same_way(summarize, tmp_path):
    # Original side: the upstream harness swallows the failure and records a plain fail.
    original_dir = _original_run(tmp_path / "o", {"ok": True, "limited": False, "slow": False})
    (original_dir / "ok" / "trajectory.json").write_text(json.dumps([SUCCESS]))
    (original_dir / "limited" / "trajectory.json").write_text(json.dumps([API_ERROR]))
    slow = json.loads((original_dir / "slow" / "result.json").read_text())
    slow["metadata"] = {"timed_out": True}
    (original_dir / "slow" / "result.json").write_text(json.dumps(slow))
    (original_dir / "slow" / "trajectory.json").write_text(json.dumps([]))
    _, original_errors = summarize.load_original(original_dir)

    # Harbor side: the same failure surfaces as a non-zero agent exit.
    harbor_dir = _harbor_job(
        tmp_path / "h",
        {"ok": True, "limited": False, "slow": False},
        exceptions={"limited": "NonZeroAgentExitCodeError", "slow": "AgentTimeoutError"},
    )
    by_task = {
        json.loads((d / "result.json").read_text())["task_name"].split("/")[-1]: d
        for d in harbor_dir.iterdir()
    }
    for task, event in (("ok", SUCCESS), ("limited", API_ERROR)):
        (by_task[task] / "agent").mkdir()
        (by_task[task] / "agent" / "claude-code.txt").write_text("banner\n" + json.dumps(event) + "\n")
    record = json.loads((by_task["limited"] / "result.json").read_text())
    record["verifier_result"] = {"rewards": {"reward": 0.0}}
    (by_task["limited"] / "result.json").write_text(json.dumps(record))
    _, harbor_errors = summarize.load_harbor(harbor_dir)

    assert [e.split(" ")[0] for e in original_errors] == ["limited"]
    assert [e.split(" ")[0] for e in harbor_errors] == ["limited"]


def test_latest_trial_wins_when_a_job_was_resumed(summarize, tmp_path):
    job = tmp_path / "job"
    for name, finished, reward in (("first", "2026-01-01T00:00:00", 0.0), ("second", "2026-01-01T01:00:00", 1.0)):
        directory = job / name
        directory.mkdir(parents=True)
        (directory / "result.json").write_text(
            json.dumps(
                {
                    "task_name": "latchbio/t",
                    "finished_at": finished,
                    "verifier_result": {"rewards": {"reward": reward}},
                    "exception_info": None,
                }
            )
        )
    results, errors = summarize.load_harbor(job)
    assert results == {"t": True} and errors == []
