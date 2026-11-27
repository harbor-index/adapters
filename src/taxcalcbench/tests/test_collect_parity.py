"""parity/collect_parity.py: statistics, coverage and readiness checks.

Uses small synthetic run folders; no upstream checkout or API needed.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "parity" / "collect_parity.py"
CASES = ["t1", "t2", "t3"]


def _load():
    spec = importlib.util.spec_from_file_location("collect_parity", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _orig_case(run_dir: Path, task: str, correct: int, total: int, status: str | None = "ok", near: int = 0) -> None:
    """near: how many of the wrong lines are within $5 of the expected amount (lenient-correct)."""
    d = run_dir / task
    d.mkdir(parents=True)
    if status is not None:  # the launcher's own record for the case
        (d / "status").write_text(f"{task}\t1\t0\tyes\tyes\t{status}\n")
    lines = [f"Line {i}: ✓ correct, expected: 1.0, actual: 1.0" for i in range(correct)]
    lines += [f"Line {i}: ✗ incorrect, expected: 1.0, actual: {3.0 if i < correct + near else 10.0}"
              for i in range(correct, total)]
    pct, lenient = 100 * correct / total, correct + near
    lines += ["", f"Strictly correct return: {correct == total}", f"Lenient correct return: {lenient == total}",
              f"Correct (by line): {pct:.2f}%", f"Correct (by line, lenient): {100 * lenient / total:.2f}%"]
    (d / "evaluation_result_high_1.md").write_text("\n".join(lines), encoding="utf-8")


META = {"outcome": "ok", "stream": False, "upstream_commit": "abc", "model": "openai/m",
        "thinking_level": "high", "api_base": "https://host-a/v1", "agent": "taxcalcbench-direct@1.0.0",
        "litellm_version": "1.100.5", "openai_version": "2.43.0", "harbor_version": "0.27.0",
        "reasoning": {"effort": "high"}, "timeout": None, "n_pdfs": 0}
FAILED_CALL = {**META, "outcome": "api_failure"}
_trial_counter = iter(range(1, 10**6))


def _harbor_trial(job_dir: Path, task: str, correct: int, total: int, *, valid=True, exc=None, suffix="a",
                  found=True, meta=META, checksum="same", graded="the reply", trial_id=None, scored=True,
                  job_id=None, near=0) -> None:
    """One trial folder. By default: a delivered, scored reply and a complete request record."""
    d = job_dir / f"{task}__{suffix}"
    d.mkdir(parents=True)
    if meta is not None:
        (d / "agent").mkdir()
        (d / "agent" / "request_meta.json").write_text(json.dumps(meta))
        if meta.get("outcome") == "ok":
            (d / "agent" / "return.txt").write_text("the reply")
    if graded is not None and meta is not None and meta.get("outcome") == "ok":
        (d / "artifacts" / "app" / "output").mkdir(parents=True)
        (d / "artifacts" / "app" / "output" / "return.txt").write_text(graded)
    if job_id is None and (job_dir / "result.json").is_file():
        job_id = json.loads((job_dir / "result.json").read_text()).get("id")
    (d / "result.json").write_text(json.dumps({
        "id": trial_id or f"trial-{next(_trial_counter)}",
        "task_name": f"column-tax/taxcalcbench__{task}", "reward_valid": valid,
        "task_checksum": f"{checksum}-{task}" if checksum else None,
        "config": {"job_id": job_id} if job_id else {},
        "exception_info": {"exception_type": exc} if exc else None}))
    if scored:
        (d / "verifier").mkdir()
        lenient = correct + near
        (d / "verifier" / "score.json").write_text(json.dumps({
            "strictly_correct_return": correct == total, "correct_by_line_score": correct / total,
            "lenient_correct_return": lenient == total, "lenient_correct_by_line_score": lenient / total,
            "output_found": found,
            "lines": [{"expected": 1.0, "actual": 1.0 if i < correct else 3.0 if i < lenient else 10.0,
                       "correct": i < correct, "lenient_correct": i < lenient} for i in range(total)]}))


def _failed_trial(job_dir: Path, task: str, exc="UnknownApiError", **kwargs) -> None:
    """A trial whose model call failed: the harness still graded the missing answer as an invalid 0."""
    _harbor_trial(job_dir, task, 0, 3, valid=False, exc=exc, found=False, meta=FAILED_CALL, **kwargs)


def _setup(tmp_path: Path, orig: list[dict], harbor: list[dict], model="openai/m"):
    cases_file = tmp_path / "cases.txt"
    cases_file.write_text("# task_id tax_year upstream_case\n" + "\n".join(f"{t} ty24 {t}" for t in CASES) + "\n")
    cases_sha = hashlib.sha256(cases_file.read_bytes()).hexdigest()
    for i, run in enumerate(orig, 1):
        run_dir = tmp_path / "orig" / f"run_{i}"
        for task, (c, t, *near) in run.items():
            _orig_case(run_dir, task, c, t, near=near[0] if near else 0)
        (run_dir / "run.settings").write_text(
            f"upstream_commit=abc\nmodel={model}\nthinking_level=high\nreasoning_effort=high\napi_base=https://host-a/v1\n"
            f"litellm=1.100.5\nopenai=2.43.0\ncases_sha256={cases_sha}\nlimit=0\n")
        (run_dir / "run.meta").write_text(f"started: run {i}\n")
        (run_dir / "run.id").write_text(f"run-id-{i}\n")
    jobs = []
    for i, run in enumerate(harbor, 1):
        job = tmp_path / f"job{i}"
        job.mkdir()
        (job / "result.json").write_text(json.dumps({"id": f"job-id-{i}", "started_at": f"2026-01-0{i}T10:00:00"}))
        (job / "config.json").write_text(json.dumps({"agents": [{"import_path": "a:B", "model_name": model,
                                                                 "kwargs": {"thinking_level": "high"}}]}))
        for task, (c, t, *near) in run.items():
            _harbor_trial(job, task, c, t, near=near[0] if near else 0)
        jobs.append(job)
    return cases_file, jobs


def _run(tmp_path: Path, cases_file: Path, jobs: list, extra: list[str] | None = None):
    out = tmp_path / "out.json"
    originals = sorted(str(d) for d in (tmp_path / "orig").glob("run_*"))
    proc = subprocess.run([sys.executable, str(SCRIPT), "--original", *originals, "--harbor",
                           *map(str, jobs), "--cases", str(cases_file), "--json", str(out), *(extra or [])],
                          capture_output=True, text=True)
    return proc, (json.loads(out.read_text()) if out.exists() else None)


FULL = {"t1": (3, 3), "t2": (1, 3), "t3": (3, 3)}
ALT = {"t1": (3, 3), "t2": (3, 3), "t3": (0, 3)}
THIRD = {"t1": (1, 3), "t2": (1, 3), "t3": (1, 3)}
ZERO = {"t1": (0, 3), "t2": (0, 3), "t3": (0, 3)}


def test_formulas_match_the_spec():
    m = _load()
    mean, sem = m._mean_sem([30.0, 40.0, 50.0])
    assert mean == 40.0 and sem == pytest.approx(math.sqrt(200 / 6))
    assert m._mean_sem([10.0]) == (10.0, None)
    assert m._overlap([30, 40], [40, 50]) and not m._overlap([30, 39.99], [40, 50])


def test_match_with_full_precision(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    proc, report = _run(tmp_path, cases_file, jobs, ["--export-dir", str(tmp_path / "rc")])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert report["complete"] and report["parity_match"]
    assert report["overlap"] == {"strict": True, "by_line": True, "lenient_strict": True, "lenient_by_line": True}
    # By-line of FULL is (100 + 33.33.. + 100) / 3: computed from marks, not the rounded "33.33%".
    assert report["original"]["by_line_runs"][0] == pytest.approx(700 / 9)
    assert report["original"]["strict_runs"] == pytest.approx([200 / 3, 200 / 3])
    exported = sorted(p.name for p in (tmp_path / "rc").iterdir())
    assert exported == ["result_harbor_trial1.json", "result_harbor_trial2.json",
                        "result_original_trial1.json", "result_original_trial2.json"]
    first = json.loads((tmp_path / "rc" / "result_harbor_trial1.json").read_text())
    assert first["n_scored"] == 3 and first["per_task"]["t2"]["correct"] == 1


def test_identical_outcomes_give_identical_floats(tmp_path):
    # Same per-case outcomes on both sides must give bit-identical run scores,
    # so the overlap check cannot fail on rounding.
    cases_file, jobs = _setup(tmp_path, [THIRD, THIRD], [THIRD, THIRD])
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert report["original"]["by_line_runs"] == report["harbor"]["by_line_runs"]
    assert report["overlap"]["by_line"] is True


def test_complete_but_non_overlapping_is_not_a_match(tmp_path):
    cases_file, jobs = _setup(tmp_path, [{t: (3, 3) for t in CASES}] * 2, [ZERO, ZERO])
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 3
    assert report["complete"] is True and report["parity_match"] is False
    assert "NO MATCH" in proc.stdout


def test_single_run_is_incomplete(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL], [FULL])
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 1 and not report["complete"] and not report["parity_match"]
    assert any("fewer than 2 runs" in p for p in report["problems"])


def test_same_job_twice_is_rejected(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL])
    proc, report = _run(tmp_path, cases_file, [jobs[0], jobs[0]])
    assert proc.returncode != 0 and report is None
    assert "more than once" in proc.stderr


def test_invalid_reward_and_missing_cases_are_not_scored(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, {"t1": (3, 3), "t2": (3, 3)}], [FULL, {"t1": (3, 3), "t3": (0, 3)}])
    # Harbor run 2: t2 failed at the API; Harbor wrote a diagnostic 0 and marked it invalid.
    _failed_trial(jobs[1], "t2")
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 1 and not report["complete"]
    assert report["runs"]["harbor"][1]["unscored"] == {"t2": "UnknownApiError"}
    assert report["runs"]["original"][1]["unscored"] == {"t3": "no evaluation file"}
    # The invalid 0 is not in the mean: Harbor run 2 = t1 strict, t3 not strict.
    assert report["harbor"]["strict_runs"][1] == pytest.approx(50.0)


def test_a_trial_without_a_reply_is_unscored_even_with_a_valid_reward(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, {"t1": (3, 3), "t3": (0, 3)}])
    # The harness graded this trial as a valid 0 although the call was cut off by the time limit.
    _harbor_trial(jobs[1], "t2", 0, 3, valid=True, exc="AgentTimeoutError", found=False,
                  meta={**META, "outcome": "cancelled"})
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 1
    assert report["runs"]["harbor"][1]["unscored"] == {"t2": "AgentTimeoutError"}


def test_a_delivered_and_scored_reply_counts_despite_a_later_error(tmp_path):
    # The reply arrived and was scored; a container step after the agent then
    # raised RuntimeError, which the parity config marks as an invalid reward.
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, {"t1": (3, 3), "t3": (0, 3)}])
    _harbor_trial(jobs[1], "t2", 3, 3, valid=False, exc="RuntimeError")
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert report["runs"]["harbor"][1]["per_task"]["t2"]["correct"] == 3
    assert any("t2: counted" in w and "RuntimeError" in w for w in report["warnings"])


@pytest.mark.parametrize("graded", ["something else", None])
def test_a_score_for_another_file_than_the_reply_does_not_count(tmp_path, graded):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, {"t1": (3, 3), "t3": (0, 3)}])
    _harbor_trial(jobs[1], "t2", 3, 3, graded=graded)
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 1
    assert "not the reply the agent recorded" in report["runs"]["harbor"][1]["unscored"]["t2"]


def test_duplicate_trials_for_one_task_are_rejected(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    _harbor_trial(jobs[0], "t1", 0, 3, suffix="b")
    proc, _ = _run(tmp_path, cases_file, jobs)
    assert proc.returncode != 0 and "more than one trial" in proc.stderr


def test_inconsistent_score_files_are_rejected(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    score = jobs[0] / "t1__a" / "verifier" / "score.json"
    score.write_text('{"strictly_correct_return": false, "correct_by_line_score": 1.0, "output_found": true, '
                     '"lines": [{"correct": true}]}')
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode != 0 and report is None and "disagree" in proc.stderr
    f = tmp_path / "orig" / "run_1" / "t2" / "evaluation_result_high_1.md"
    import shutil

    shutil.rmtree(jobs[0] / "t1__a")
    _harbor_trial(jobs[0], "t1", 3, 3, suffix="c")
    f.write_text(f.read_text().replace("Correct (by line): 33.33%", "Correct (by line): 66.67%"))
    proc, _ = _run(tmp_path, cases_file, jobs)
    assert proc.returncode != 0 and "disagree" in proc.stderr


def test_runs_with_different_settings_are_incomplete(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    (jobs[1] / "config.json").write_text(json.dumps({"agents": [{"import_path": "a:B", "model_name": "openai/other"}]}))
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 1
    assert any("different settings" in p for p in report["problems"])


def test_missing_records_can_never_give_a_match(tmp_path):
    # Identical scores on both sides, but one Harbor trial has no request record.
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    (jobs[0] / "t1__a" / "agent" / "request_meta.json").unlink()
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 1 and report["parity_match"] is False
    assert "no complete request record" in report["runs"]["harbor"][0]["unscored"]["t1"]


def test_original_run_without_settings_is_incomplete(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    (tmp_path / "orig" / "run_2" / "run.settings").unlink()
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 1 and report["parity_match"] is False
    assert any("no complete record of its settings" in p for p in report["problems"])


def test_export_replaces_an_earlier_longer_export(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    rc = tmp_path / "rc"
    rc.mkdir()
    (rc / "result_harbor_trial9.json").write_text("{}")
    proc, _ = _run(tmp_path, cases_file, jobs, ["--export-dir", str(rc)])
    assert proc.returncode == 0
    assert not (rc / "result_harbor_trial9.json").exists() and len(list(rc.iterdir())) == 4


def test_retry_job_fills_only_failed_cases(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, {"t1": (3, 3), "t3": (0, 3)}])
    _failed_trial(jobs[1], "t2")
    retry = tmp_path / "job2-retry"
    retry.mkdir()
    (retry / "result.json").write_text(json.dumps({"id": "job-id-retry", "started_at": "2026-01-09T10:00:00"}))
    (retry / "config.json").write_text((jobs[1] / "config.json").read_text())
    _harbor_trial(retry, "t2", 3, 3)
    proc, report = _run(tmp_path, cases_file, [jobs[0], f"{jobs[1]},{retry}"])
    assert proc.returncode == 0 and report["parity_match"], proc.stdout + proc.stderr
    assert report["harbor"]["strict_runs"][1] == pytest.approx(200 / 3)
    # A retry can never replace a score: the first valid attempt of t1 (3/3) counts,
    # the later one (0/3) is listed and ignored.
    _harbor_trial(retry, "t1", 0, 3)
    proc, report = _run(tmp_path, cases_file, [jobs[0], f"{jobs[1]},{retry}"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    record = report["runs"]["harbor"][1]
    assert record["per_task"]["t1"]["correct"] == 3
    assert any("t1: the attempt in job2-retry is ignored" in w for w in report["warnings"])
    ignored = [a for a in record["attempts"] if a["task"] == "t1" and a["job"] == "job2-retry"]
    assert ignored and ignored[0]["counted"] is False


def test_lost_answer_is_an_infrastructure_failure(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, {"t1": (3, 3), "t3": (0, 3)}])
    # The agent wrote a reply, but the verifier saw no answer file.
    _harbor_trial(jobs[1], "t2", 0, 3, found=False)
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 1
    assert "did not reach the verifier" in report["runs"]["harbor"][1]["unscored"]["t2"]


def test_sides_with_different_settings_are_incomplete(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 0, proc.stdout + proc.stderr  # same settings on both sides
    meta = next(p for p in jobs[0].iterdir() if p.is_dir()) / "agent" / "request_meta.json"
    meta.write_text(meta.read_text().replace("host-a", "host-b"))
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 1
    assert any("did not use the same" in p for p in report["problems"])


# ---- the runs must be the ones the report says they are ----
UP = {"t1": (1, 7), "t2": (3, 7), "t3": (2, 7)}
DOWN = {"t1": (2, 7), "t2": (3, 7), "t3": (1, 7)}


def test_equal_scores_in_another_order_still_overlap(tmp_path):
    # (1/7 + 3/7 + 2/7) / 3 on one side, (2/7 + 3/7 + 1/7) / 3 on the other: the
    # same number, which floating point adds up to two different values.
    def added_as_floats(side):
        total = 0.0
        for correct, lines in side.values():
            total = total + 100 * correct / lines
        return total / 3

    assert added_as_floats(UP) != added_as_floats(DOWN)  # why exact fractions are needed
    cases_file, jobs = _setup(tmp_path, [UP, UP], [DOWN, DOWN])
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert report["overlap"] == {"strict": True, "by_line": True, "lenient_strict": True, "lenient_by_line": True}


def test_a_shorter_case_list_cannot_turn_into_a_match(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, FULL], [FULL, FULL])
    short = tmp_path / "short.txt"
    short.write_text("t1 ty24 t1\nt3 ty24 t3\n")
    proc, report = _run(tmp_path, short, jobs)
    assert proc.returncode == 1 and not report["parity_match"]
    assert any("different case list" in p for p in report["problems"])
    assert any("outside the case list" in p for p in report["problems"])


def test_a_run_made_with_limit_is_rejected(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    settings = tmp_path / "orig" / "run_1" / "run.settings"
    settings.write_text(settings.read_text().replace("limit=0", "limit=2"))
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode != 0 and report is None and "LIMIT" in proc.stderr


def test_a_copy_of_a_job_or_run_is_not_a_second_run(tmp_path):
    import shutil

    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    copy = tmp_path / "job1-copy"
    shutil.copytree(jobs[0], copy)
    proc, report = _run(tmp_path, cases_file, [jobs[0], copy])
    assert proc.returncode == 1 and any("more than once" in p for p in report["problems"])
    # The same for an original run copied under another name, even after a rerun
    # of the copy added lines to its records: the launcher's run id stays.
    shutil.rmtree(tmp_path / "orig" / "run_2")
    shutil.copytree(tmp_path / "orig" / "run_1", tmp_path / "orig" / "run_2")
    with open(tmp_path / "orig" / "run_2" / "run.meta", "a") as f:
        f.write("--- rerun of failed cases ---\nstarted: later\n")
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 1 and any("more than once" in p for p in report["problems"])


def test_harbor_runs_on_different_task_files_are_incomplete(tmp_path):
    import shutil

    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    shutil.rmtree(jobs[1] / "t2__a")
    _harbor_trial(jobs[1], "t2", 3, 3, checksum="regenerated")
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 1
    assert any("t2 ran on different task files" in p for p in report["problems"])


def test_two_served_models_are_incomplete(tmp_path):
    import shutil

    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    shutil.rmtree(jobs[0] / "t1__a")
    _harbor_trial(jobs[0], "t1", 3, 3, meta={**META, "served_model": "m-2026-01"})
    shutil.rmtree(jobs[1] / "t1__a")
    _harbor_trial(jobs[1], "t1", 3, 3, meta={**META, "served_model": "m-2026-06"})
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 1
    assert any("more than one model name" in p for p in report["problems"])


def test_level_must_be_the_one_the_runs_used(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    for run in (tmp_path / "orig").glob("run_*"):
        for f in run.glob("*/evaluation_result_high_1.md"):
            f.rename(f.with_name("evaluation_result_medium_1.md"))
    proc, report = _run(tmp_path, cases_file, jobs, ["--level", "medium"])
    assert proc.returncode == 1
    assert any("--level medium differs" in p for p in report["problems"])


def test_an_aborted_collection_leaves_no_earlier_report(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 0 and report is not None
    proc, report = _run(tmp_path, tmp_path / "missing-cases.txt", jobs)
    assert proc.returncode != 0 and report is None  # the earlier out.json is gone


def test_two_agent_versions_are_incomplete(tmp_path):
    import shutil

    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    shutil.rmtree(jobs[1] / "t1__a")
    _harbor_trial(jobs[1], "t1", 3, 3, meta={**META, "agent": "taxcalcbench-direct@0.9.0"})
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 1
    assert any("more than one agent version" in p for p in report["problems"])


def test_report_goes_into_a_new_folder_with_provenance(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    out = tmp_path / "new" / "folder" / "summary.json"
    originals = sorted(str(d) for d in (tmp_path / "orig").glob("run_*"))
    proc = subprocess.run([sys.executable, str(SCRIPT), "--original", *originals, "--harbor", *map(str, jobs),
                           "--cases", str(cases_file), "--json", str(out)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    record = json.loads(out.read_text())["runs"]["harbor"][0]
    assert record["provenance"] == [{"upstream_commit": "abc", "model": "openai/m", "thinking_level": "high",
                                     "reasoning_effort": "high", "api_base": "https://host-a/v1", "litellm": "1.100.5",
                                     "openai": "2.43.0"}]


def test_a_folder_that_is_no_trial_result_gives_a_clear_message(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    stray = jobs[0] / "notes"
    stray.mkdir()
    (stray / "result.json").write_text("{}")
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode != 0 and report is None and "no task_name" in proc.stderr


def test_a_retry_on_changed_task_files_is_incomplete(tmp_path):
    # The first attempt of t2 failed on other task files than every scored trial used
    # (the task was regenerated before the retry): only the failed attempt can tell.
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, {"t1": (3, 3), "t3": (0, 3)}])
    _failed_trial(jobs[1], "t2", checksum="before-regeneration")
    retry = tmp_path / "job2-retry"
    retry.mkdir()
    (retry / "result.json").write_text(json.dumps({"id": "job-id-retry", "started_at": "2026-01-09T10:00:00"}))
    (retry / "config.json").write_text((jobs[1] / "config.json").read_text())
    _harbor_trial(retry, "t2", 3, 3)
    proc, report = _run(tmp_path, cases_file, [jobs[0], f"{jobs[1]},{retry}"])
    assert proc.returncode == 1
    assert any("t2 ran on different task files" in p for p in report["problems"])


def test_copied_trials_are_found_without_a_job_id(tmp_path):
    import shutil

    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    copy = tmp_path / "job1-copy"
    shutil.copytree(jobs[0], copy)
    for job in (jobs[0], copy):
        (job / "result.json").unlink()  # no job-level identity left
    proc, report = _run(tmp_path, cases_file, [jobs[0], copy])
    assert proc.returncode == 1
    assert any("more than once" in p for p in report["problems"])
    assert any("no job id" in p for p in report["problems"])


def test_changed_request_parameters_are_incomplete(tmp_path):
    import shutil

    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    shutil.rmtree(jobs[1] / "t1__a")
    _harbor_trial(jobs[1], "t1", 3, 3, meta={**META, "reasoning": {"effort": "medium"}})
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 1
    assert any("t1 were sent with different request parameters" in p for p in report["problems"])
    assert any("more than one reasoning setting" in p for p in report["problems"])


def test_job_configuration_must_agree_with_the_request_records(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    for job in jobs:  # the same (wrong) level in every job config: only the request records can tell
        (job / "config.json").write_text(json.dumps({"agents": [{"import_path": "a:B", "model_name": "openai/m",
                                                                 "kwargs": {"thinking_level": "low"}}]}))
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 1
    assert any("the job configuration says" in p for p in report["problems"])


@pytest.mark.parametrize("key, value, message", [
    ("litellm_version", "1.99.0", "did not use the same"),
    ("litellm_version", None, "no complete request record"),
    ("harbor_version", "0.26.0", "more than one Harbor version"),
])
def test_runtime_versions_are_compared(tmp_path, key, value, message):
    import shutil

    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    shutil.rmtree(jobs[1] / "t1__a")
    _harbor_trial(jobs[1], "t1", 3, 3, meta={**META, key: value})
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 1
    assert message in json.dumps(report["problems"]) + json.dumps(report["runs"]["harbor"][1]["unscored"])


def test_a_run_without_any_score_still_gets_a_report(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, {}])
    for task in CASES:
        _failed_trial(jobs[1], task)
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 1 and report is not None
    assert report["harbor"]["strict_runs"][1] is None and report["overlap"] == {"strict": None, "by_line": None, "lenient_strict": None, "lenient_by_line": None}
    assert any("no scored case" in p for p in report["problems"])


# ---- what an interrupted or mislabelled attempt may never do ----
@pytest.mark.parametrize("status, reason", [("in_progress", "status in_progress"), (None, "no status record")])
def test_original_case_counts_only_with_the_launchers_ok(tmp_path, status, reason):
    # Upstream's files are all there (the attempt was cut off after they were moved), but the
    # launcher never recorded the case as ok.
    import shutil

    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    run_dir = tmp_path / "orig" / "run_2"
    shutil.rmtree(run_dir / "t2")
    _orig_case(run_dir, "t2", 3, 3, status=status)
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 1
    assert reason in report["runs"]["original"][1]["unscored"]["t2"]


def test_reasoning_effort_is_compared_across_sides(tmp_path):
    # Every Harbor request went out with another effort than the original side used,
    # while both configurations say thinking level high.
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [{}, {}])
    for job, run in zip(jobs, (FULL, ALT)):
        for task, (c, t) in run.items():
            _harbor_trial(job, task, c, t, meta={**META, "reasoning": {"effort": "medium"}})
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 1
    assert any("did not use the same" in p for p in report["problems"])


@pytest.mark.parametrize("change, message", [
    ({"n_pdfs": 2, "stream": True}, "do not fit a ty24 case"),
    ("drop n_pdfs", "no complete request record"),
])
def test_request_parameters_must_be_recorded_and_fit_the_tax_year(tmp_path, change, message):
    import shutil

    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    for job in jobs:
        shutil.rmtree(job / "t1__a")
        meta = {k: v for k, v in META.items() if k != "n_pdfs"} if isinstance(change, str) else {**META, **change}
        _harbor_trial(job, "t1", 3, 3, meta=meta)
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 1
    assert message in json.dumps(report["problems"]) + json.dumps(report["runs"]["harbor"][0]["unscored"])


def test_ty25_case_needs_a_streamed_request_with_pdfs(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    cases_file.write_text(cases_file.read_text().replace("t1 ty24 t1", "t1 ty25 t1"))
    for run in (tmp_path / "orig").glob("run_*"):  # the runs were made with this list
        settings = run / "run.settings"
        settings.write_text(settings.read_text().replace(
            settings.read_text().split("cases_sha256=")[1].split("\n")[0],
            hashlib.sha256(cases_file.read_bytes()).hexdigest()))
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 1
    assert any("t1: request parameters do not fit a ty25 case" in p for p in report["problems"])


def test_a_failed_attempt_with_other_versions_is_incomplete_and_stays_in_the_report(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, {"t1": (3, 3), "t3": (0, 3)}])
    _failed_trial(jobs[1], "t2")
    retry = tmp_path / "job2-retry"
    retry.mkdir()
    (retry / "result.json").write_text(json.dumps({"id": "job-id-retry", "started_at": "2026-01-09T10:00:00"}))
    (retry / "config.json").write_text((jobs[1] / "config.json").read_text())
    _harbor_trial(retry, "t2", 3, 3)
    proc, report = _run(tmp_path, cases_file, [jobs[0], f"{jobs[1]},{retry}"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    attempts = [a for a in report["runs"]["harbor"][1]["attempts"] if a["task"] == "t2"]
    assert [(a["job"], a["counted"], a["exception"]) for a in attempts] == [
        ("job2", False, "UnknownApiError"), ("job2-retry", True, None)]
    # The same, but the failed attempt was made with another LiteLLM version.
    import shutil

    shutil.rmtree(jobs[1] / "t2__a")
    _harbor_trial(jobs[1], "t2", 0, 3, valid=False, exc="UnknownApiError", found=False,
                  meta={**FAILED_CALL, "litellm_version": "1.99.0"})
    proc, report = _run(tmp_path, cases_file, [jobs[0], f"{jobs[1]},{retry}"])
    assert proc.returncode == 1
    assert any("did not use the same" in p for p in report["problems"])


def test_a_trial_from_another_job_is_incomplete(tmp_path):
    import shutil

    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    shutil.rmtree(jobs[1] / "t1__a")
    _harbor_trial(jobs[1], "t1", 3, 3, job_id="some-other-job")
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 1
    assert any("names job some-other-job" in p for p in report["problems"])


# ---- definitions of the two metrics and of a match ----
def test_by_line_is_the_mean_of_per_case_shares(tmp_path):
    # Cases with different numbers of scored lines: (1/2 + 4/4 + 0/10) / 3 = 50 %.
    # Pooling the lines would give 5/16 = 31.25 %.
    uneven = {"t1": (1, 2), "t2": (4, 4), "t3": (0, 10)}
    cases_file, jobs = _setup(tmp_path, [uneven, uneven], [uneven, uneven])
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert report["original"]["by_line_runs"] == [50.0, 50.0] == report["harbor"]["by_line_runs"]
    assert report["original"]["strict_runs"] == pytest.approx([100 / 3, 100 / 3])


def test_every_metric_must_overlap(tmp_path):
    # Strict score equal on both sides (1 of 3), by-line far apart.
    low = {"t1": (3, 3), "t2": (0, 3), "t3": (0, 3)}
    high = {"t1": (3, 3), "t2": (2, 3), "t3": (2, 3)}
    cases_file, jobs = _setup(tmp_path, [low, low], [high, high])
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 3
    assert report["overlap"] == {"strict": True, "by_line": False, "lenient_strict": True, "lenient_by_line": False} and report["parity_match"] is False


def test_unequal_run_counts_are_incomplete(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT, FULL], [FULL, ALT])
    (tmp_path / "orig" / "run_3" / "run.id").write_text("run-id-3\n")
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 1 and any("unequal run counts" in p for p in report["problems"])


def test_invalid_reward_without_an_error_is_not_counted(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, {"t1": (3, 3), "t3": (0, 3)}])
    _harbor_trial(jobs[1], "t2", 3, 3, valid=False)
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 1
    assert report["runs"]["harbor"][1]["unscored"] == {"t2": "reward marked invalid"}


def test_a_trial_outside_the_case_list_is_incomplete(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    _harbor_trial(jobs[0], "t9", 3, 3)
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 1
    assert any("harbor job1 contains cases outside the case list: t9" in p for p in report["problems"])


def test_scored_line_totals_must_agree_everywhere(tmp_path):
    other_total = {"t1": (3, 3), "t2": (1, 4), "t3": (3, 3)}
    cases_file, jobs = _setup(tmp_path, [FULL, FULL], [other_total, other_total])
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 1
    assert any("t2: the number of scored lines differs" in p for p in report["problems"])


def test_an_oversized_answer_is_incomplete(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    score = jobs[0] / "t1__a" / "verifier" / "score.json"
    score.write_text(json.dumps({**json.loads(score.read_text()), "output_too_large": True}))
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 1
    assert any("answer over the scorer's size limit" in p for p in report["problems"])


@pytest.mark.parametrize("victim", ["t1__a/result.json", "t1__a/verifier/score.json", "t1__a/agent/request_meta.json",
                                    "config.json"])
def test_unreadable_files_give_a_message_and_no_report(tmp_path, victim):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    (jobs[0] / victim).write_text("{not json")
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 1 and report is None
    assert "not valid JSON" in proc.stderr and "Traceback" not in proc.stderr


def test_the_verdict_is_printed_only_after_the_report_is_written(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    blocker = tmp_path / "a-file"
    blocker.write_text("")
    originals = sorted(str(d) for d in (tmp_path / "orig").glob("run_*"))
    proc = subprocess.run([sys.executable, str(SCRIPT), "--original", *originals, "--harbor", *map(str, jobs),
                           "--cases", str(cases_file), "--json", str(blocker / "summary.json")],
                          capture_output=True, text=True)
    assert proc.returncode == 1 and "PARITY MATCH" not in proc.stdout
    assert "cannot write the report" in proc.stderr and "Traceback" not in proc.stderr


# ---- rules named by the reviewers that had no test ----
def test_strict_must_overlap_too(tmp_path):
    # By-line equal on both sides (one third of the lines), strict apart (1 of 3 against 0 of 3).
    one_strict = {"t1": (3, 3), "t2": (0, 3), "t3": (0, 3)}
    cases_file, jobs = _setup(tmp_path, [one_strict, one_strict], [THIRD, THIRD])
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert report["overlap"] == {"strict": False, "by_line": True, "lenient_strict": False, "lenient_by_line": True} and report["parity_match"] is False


def test_a_trial_that_names_no_job_is_incomplete(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    result = jobs[1] / "t1__a" / "result.json"
    result.write_text(json.dumps({**json.loads(result.read_text()), "config": {}}))
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 1
    assert any("t1: the trial's result.json names no job" in p for p in report["problems"])


def _as_ty25(tmp_path, cases_file):
    """Turn t1 into a TY2025 row of the case list the runs were made with."""
    cases_file.write_text(cases_file.read_text().replace("t1 ty24 t1", "t1 ty25 t1"))
    for run in (tmp_path / "orig").glob("run_*"):
        settings = run / "run.settings"
        old = settings.read_text().split("cases_sha256=")[1].split("\n")[0]
        settings.write_text(settings.read_text().replace(old, hashlib.sha256(cases_file.read_bytes()).hexdigest()))


@pytest.mark.parametrize("change, fits", [({"stream": True, "n_pdfs": 2}, True),
                                          ({"stream": False, "n_pdfs": 2}, False),
                                          ({"stream": True, "n_pdfs": 0}, False)])
def test_ty25_fit_checks_stream_and_pdf_count_separately(tmp_path, change, fits):
    import shutil

    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    _as_ty25(tmp_path, cases_file)
    for job in jobs:
        shutil.rmtree(job / "t1__a")
        _harbor_trial(job, "t1", 3, 3, meta={**META, **change})
    proc, report = _run(tmp_path, cases_file, jobs)
    assert (proc.returncode == 0) is fits, proc.stdout + proc.stderr
    assert any("t1: request parameters do not fit a ty25 case" in p for p in report["problems"]) is not fits


@pytest.mark.parametrize("victim", ["t1__a/result.json", "t1__a/verifier/score.json", "t1__a/agent/request_meta.json",
                                    "config.json", "result.json"])
@pytest.mark.parametrize("content, message", [(b"[1, 2]", "expected a JSON object"),
                                              (b"\xff\xfe{}", "not readable as UTF-8 text")])
def test_a_json_list_or_non_utf8_file_gives_a_message_and_no_report(tmp_path, victim, content, message):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    (jobs[0] / victim).write_bytes(content)
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 1 and report is None
    assert message in proc.stderr and "Traceback" not in proc.stderr


def test_an_unreadable_case_list_or_folder_gives_a_message(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    reply = jobs[0] / "t1__a" / "agent" / "return.txt"
    reply.unlink()
    reply.mkdir()  # a folder where the reply file should be
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 1 and "Traceback" not in proc.stderr
    cases_file.write_bytes(b"\xff\xfe t1 ty24 t1\n")
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 1 and report is None
    assert "not readable as UTF-8 text" in proc.stderr and "Traceback" not in proc.stderr


def test_runs_without_a_run_id_are_identified_by_their_records(tmp_path):
    import shutil

    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    for run in (tmp_path / "orig").glob("run_*"):
        (run / "run.id").unlink()
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 0, proc.stdout + proc.stderr  # two different runs
    shutil.rmtree(tmp_path / "orig" / "run_2")
    shutil.copytree(tmp_path / "orig" / "run_1", tmp_path / "orig" / "run_2")
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 1 and any("more than once" in p for p in report["problems"])


def test_original_attempts_are_listed_in_the_report(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    (tmp_path / "orig" / "run_1" / "t2" / "attempt_1").mkdir()
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert report["runs"]["original"][0]["attempts"] == [
        {"task": "t1", "status": "ok", "attempt": "1", "earlier_attempts": []},
        {"task": "t2", "status": "ok", "attempt": "1", "earlier_attempts": ["attempt_1"]},
        {"task": "t3", "status": "ok", "attempt": "1", "earlier_attempts": []}]


def _retry_job(tmp_path, jobs, started_at="2026-01-09T10:00:00"):
    retry = tmp_path / "job2-retry"
    retry.mkdir()
    (retry / "result.json").write_text(json.dumps({"id": "job-id-retry", "started_at": started_at}))
    (retry / "config.json").write_text((jobs[1] / "config.json").read_text())
    return retry


@pytest.mark.parametrize("change", [{"timeout": 5}, {"stream": True}, {"n_pdfs": 3}])
def test_request_parameters_of_a_failed_attempt_are_compared_too(tmp_path, change):
    # The first attempt of t2 failed after it had recorded other request parameters than the retry used.
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, {"t1": (3, 3), "t3": (0, 3)}])
    _harbor_trial(jobs[1], "t2", 0, 3, valid=False, exc="UnknownApiError", found=False, meta={**FAILED_CALL, **change})
    retry = _retry_job(tmp_path, jobs)
    _harbor_trial(retry, "t2", 3, 3)
    proc, report = _run(tmp_path, cases_file, [jobs[0], f"{jobs[1]},{retry}"])
    assert proc.returncode == 1
    assert any("t2 were sent with different request parameters" in p for p in report["problems"])


def test_an_ignored_attempt_is_not_reported_as_counted(tmp_path):
    # Both attempts of t1 were delivered and scored and both recorded a later error; only the first one counts.
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, {"t2": (3, 3), "t3": (0, 3)}])
    _harbor_trial(jobs[1], "t1", 3, 3, valid=False, exc="RuntimeError")
    retry = _retry_job(tmp_path, jobs)
    _harbor_trial(retry, "t1", 0, 3, valid=False, exc="RuntimeError")
    proc, report = _run(tmp_path, cases_file, [jobs[0], f"{jobs[1]},{retry}"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    counted = [w for w in report["warnings"] if "t1: counted" in w]
    assert len(counted) == 1 and "(job2)" in counted[0]
    assert any("t1: the attempt in job2-retry is ignored" in w for w in report["warnings"])


def test_job_folders_of_a_run_must_be_given_in_the_order_they_were_made(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, {"t1": (3, 3), "t3": (0, 3)}])
    _failed_trial(jobs[1], "t2")
    retry = _retry_job(tmp_path, jobs)
    _harbor_trial(retry, "t2", 3, 3)
    proc, report = _run(tmp_path, cases_file, [jobs[0], f"{retry},{jobs[1]}"])  # the retry first: wrong order
    assert proc.returncode == 1
    assert any("job2 was started before job2-retry" in p for p in report["problems"])


def test_an_earlier_report_goes_even_when_a_path_is_wrong(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 0 and report["parity_match"] is True
    not_a_folder = tmp_path / "a-file"
    not_a_folder.write_text("")
    proc, report = _run(tmp_path, cases_file, jobs, ["--export-dir", str(not_a_folder)])
    assert proc.returncode == 1 and "--export-dir is not a folder" in proc.stderr
    assert report is None  # the earlier out.json, which said parity_match true, is gone


# ---- last small rules ----
def test_a_file_with_an_unexpected_shape_gives_a_message_and_no_report(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    (jobs[0] / "config.json").write_text(json.dumps({"agents": "abc"}))  # valid JSON, wrong shape
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 1 and report is None
    assert "collection stopped:" in proc.stderr and "Traceback" not in proc.stderr


def test_request_parameters_of_an_ignored_retry_are_compared_too(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    retry = _retry_job(tmp_path, jobs)
    _harbor_trial(retry, "t1", 3, 3, meta={**META, "timeout": 5})  # t1 already counts in job2
    proc, report = _run(tmp_path, cases_file, [jobs[0], f"{jobs[1]},{retry}"])
    assert proc.returncode == 1
    assert any("t1 were sent with different request parameters" in p for p in report["problems"])


def test_a_failed_attempt_that_lacks_a_parameter_is_not_skipped(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, {"t1": (3, 3), "t3": (0, 3)}])
    record = {k: v for k, v in FAILED_CALL.items() if k != "timeout"}
    _harbor_trial(jobs[1], "t2", 0, 3, valid=False, exc="UnknownApiError", found=False, meta=record)
    retry = _retry_job(tmp_path, jobs)
    _harbor_trial(retry, "t2", 3, 3)
    proc, report = _run(tmp_path, cases_file, [jobs[0], f"{jobs[1]},{retry}"])
    assert proc.returncode == 1
    assert any("t2 were sent with different request parameters" in p for p in report["problems"])


def _third_job(tmp_path, jobs, name, started_at):
    job = tmp_path / name
    job.mkdir()
    (job / "result.json").write_text(json.dumps({"id": f"id-{name}", **({"started_at": started_at} if started_at else {})}))
    (job / "config.json").write_text((jobs[1] / "config.json").read_text())
    return job


def test_three_job_folders_out_of_order(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, {"t1": (3, 3)}])
    second = _third_job(tmp_path, jobs, "job2-b", "2026-01-05T10:00:00")
    third = _third_job(tmp_path, jobs, "job2-c", "2026-01-07T10:00:00")
    _harbor_trial(second, "t2", 3, 3)
    _harbor_trial(third, "t3", 0, 3)
    proc, report = _run(tmp_path, cases_file, [jobs[0], f"{jobs[1]},{second},{third}"])
    assert proc.returncode == 0, proc.stdout + proc.stderr  # in the order they were made
    proc, report = _run(tmp_path, cases_file, [jobs[0], f"{jobs[1]},{third},{second}"])
    assert proc.returncode == 1
    assert any("job2-b was started before job2-c" in p for p in report["problems"])


def test_a_job_without_a_start_time_in_a_run_of_several_is_incomplete(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, {"t1": (3, 3), "t3": (0, 3)}])
    _failed_trial(jobs[1], "t2")
    retry = _third_job(tmp_path, jobs, "job2-retry", None)
    _harbor_trial(retry, "t2", 3, 3)
    proc, report = _run(tmp_path, cases_file, [jobs[0], f"{jobs[1]},{retry}"])
    assert proc.returncode == 1
    assert any("job2-retry: no started_at" in p for p in report["problems"])


def test_earlier_result_files_go_even_when_json_names_a_folder(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    export = tmp_path / "rc"
    proc, _ = _run(tmp_path, cases_file, jobs, ["--export-dir", str(export)])
    assert proc.returncode == 0 and len(list(export.iterdir())) == 4
    originals = sorted(str(d) for d in (tmp_path / "orig").glob("run_*"))
    a_folder = tmp_path / "summary-folder"
    a_folder.mkdir()
    proc = subprocess.run([sys.executable, str(SCRIPT), "--original", *originals, "--harbor", *map(str, jobs),
                           "--cases", str(cases_file), "--json", str(a_folder), "--export-dir", str(export)],
                          capture_output=True, text=True)
    assert proc.returncode == 1 and "--json is a folder" in proc.stderr
    assert list(export.iterdir()) == []  # the earlier result files are gone


def test_ignoring_one_retry_keeps_the_warnings_of_the_other_trials_in_that_job(tmp_path):
    # The retry job holds t2 again (already counted, so ignored) and the first valid t3,
    # whose trial recorded an error after its reply was delivered and scored.
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, {"t1": (3, 3), "t2": (3, 3)}])
    _failed_trial(jobs[1], "t3")
    retry = _retry_job(tmp_path, jobs)
    _harbor_trial(retry, "t2", 0, 3)
    _harbor_trial(retry, "t3", 0, 3, valid=False, exc="RuntimeError")
    proc, report = _run(tmp_path, cases_file, [jobs[0], f"{jobs[1]},{retry}"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert any("t2: the attempt in job2-retry is ignored" in w for w in report["warnings"])
    assert any("t3: counted" in w and "(job2-retry)" in w for w in report["warnings"])


def test_a_run_with_one_job_folder_needs_no_start_time(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    for i, job in enumerate(jobs, 1):
        (job / "result.json").write_text(json.dumps({"id": f"job-id-{i}"}))  # no started_at
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_lenient_metrics_are_read_from_the_amounts_and_compared(tmp_path):
    # Same strict and by-line results on both sides, but the original side's wrong lines are within $5
    # (lenient-correct) and Harbor's are not: the lenient metrics differ, so there is no match.
    near = {"t1": (3, 3), "t2": (1, 3, 2), "t3": (0, 3, 3)}
    far = {"t1": (3, 3), "t2": (1, 3), "t3": (0, 3)}
    cases_file, jobs = _setup(tmp_path, [near, near], [far, far])
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert report["overlap"] == {"strict": True, "by_line": True, "lenient_strict": False, "lenient_by_line": False}
    assert report["original"]["lenient_strict_runs"] == pytest.approx([100.0, 100.0])
    assert report["harbor"]["lenient_strict_runs"] == pytest.approx([100 / 3, 100 / 3])
    assert report["original"]["lenient_by_line_runs"][0] == pytest.approx(100.0)
    assert report["runs"]["original"][0]["correct_by_line_lenient_pct"] == pytest.approx(100.0)
    assert "NO MATCH" in proc.stdout


def test_a_wrong_lenient_summary_in_upstreams_report_is_rejected(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    f = tmp_path / "orig" / "run_1" / "t2" / "evaluation_result_high_1.md"
    f.write_text(f.read_text().replace("Correct (by line, lenient): 33.33%", "Correct (by line, lenient): 66.67%"))
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode != 0 and report is None and "lenient" in proc.stderr and "disagree" in proc.stderr


@pytest.mark.parametrize("change, message", [
    (lambda s: {**s, "lenient_correct_return": not s["lenient_correct_return"]}, "disagree with lenient_correct_return"),
    (lambda s: {**s, "lines": [{**s["lines"][0], "lenient_correct": False}] + s["lines"][1:]}, "marked not lenient-correct"),
    (lambda s: {**s, "lines": [{k: v for k, v in x.items() if k != "lenient_correct"} for x in s["lines"]]},
     "lenient results are not true/false"),
    (lambda s: {k: v for k, v in s.items() if k != "lenient_correct_return"}, "lenient_correct_return is not true/false"),
    (lambda s: {**s, "lenient_correct_return": "true"}, "lenient_correct_return is not true/false"),
    (lambda s: {**s, "lines": [{**s["lines"][0], "actual": 50.0}] + s["lines"][1:]}, "disagree with the line's own amounts"),
    (lambda s: {**s, "lines": [{k: v for k, v in x.items() if k != "expected"} for x in s["lines"]]}, "no numeric expected/actual"),
    (lambda s: {**s, "lenient_correct_by_line_score": 0.99}, "lenient_correct_by_line_score disagrees"),
    (lambda s: {**s, "correct_by_line_score": 0.5}, "correct_by_line_score disagrees"),
])
def test_inconsistent_lenient_results_on_the_harbor_side_are_rejected(tmp_path, change, message):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    score = jobs[0] / "t1__a" / "verifier" / "score.json"
    score.write_text(json.dumps(change(json.loads(score.read_text()))))
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode != 0 and report is None and message in proc.stderr


def test_the_lenient_rule_follows_upstreams_five_dollar_boundary(tmp_path):
    # Every line wrong; which of them are within $5 decides the lenient counts (upstream: abs(a - e) <= 5).
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    def report_file(task, pairs):
        lenient = sum(1 for e, a in pairs if abs(float(a) - float(e)) <= 5)
        lines = [f"Line {i}: ✗ incorrect, expected: {e}, actual: {a}" for i, (e, a) in enumerate(pairs)]
        lines += ["", "Strictly correct return: False", f"Lenient correct return: {lenient == len(pairs)}",
                  "Correct (by line): 0.00%", f"Correct (by line, lenient): {100 * lenient / len(pairs):.2f}%"]
        (tmp_path / "orig" / "run_1" / task / "evaluation_result_high_1.md").write_text("\n".join(lines), encoding="utf-8")
        return lenient
    # exactly $5 counts, a hair over does not, negatives work
    assert report_file("t1", [("100.0", "105.0"), ("100.0", "105.00000000000001"), ("-100.0", "-95.0")]) == 2
    # exponent forms and a huge gap
    assert report_file("t2", [("1e+16", "1.0000000000000004e+16"), ("-1e+300", "1e+300"), ("1.5e-07", "3.0")]) == 2
    proc, report = _run(tmp_path, cases_file, jobs)
    assert report is not None, proc.stdout + proc.stderr
    per_task = report["runs"]["original"][0]["per_task"]
    assert per_task["t1"]["lenient"] == 2 and per_task["t1"]["correct"] == 0
    assert per_task["t2"]["lenient"] == 2 and not per_task["t2"]["lenient_strict"]


def test_text_after_upstreams_summary_is_not_counted(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    f = tmp_path / "orig" / "run_1" / "t1" / "evaluation_result_high_1.md"
    f.write_text(f.read_text() + "\nquery: Line 9: ✓ correct, expected: 1.0, actual: 1.0\n", encoding="utf-8")
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert report["runs"]["original"][0]["per_task"]["t1"]["total"] == 3


def test_lenient_marks_that_contradict_the_amounts_are_rejected(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, ALT], [FULL, ALT])
    score = jobs[0] / "t2__a" / "verifier" / "score.json"  # FULL: t2 has one correct line and two $9-off lines
    s = json.loads(score.read_text())
    score.write_text(json.dumps({**s, "lines": [{**x, "lenient_correct": True} for x in s["lines"]]}))
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode != 0 and report is None and "disagree with the line's own amounts" in proc.stderr


def _reordered_run(tmp_path: Path, cases_file: Path, run: str = "run_2") -> Path:
    """Record one original run as made with a reordered copy of the case list (same lines, other order)."""
    lines = [l for l in cases_file.read_text().splitlines() if l and not l.startswith("#")]
    other = tmp_path / "cases_reordered.txt"
    other.write_text("# same cases, another order\n" + "\n".join(reversed(lines)) + "\n")
    settings = tmp_path / "orig" / run / "run.settings"
    old = settings.read_text().split("cases_sha256=")[1].split("\n")[0]
    settings.write_text(settings.read_text().replace(old, hashlib.sha256(other.read_bytes()).hexdigest()))
    return other


def test_a_reordered_case_list_counts_only_when_named(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, FULL], [FULL, FULL])
    other = _reordered_run(tmp_path, cases_file)
    proc, report = _run(tmp_path, cases_file, jobs)
    assert proc.returncode == 1 and not report["parity_match"]
    assert any("different case list" in p for p in report["problems"])
    assert not any("different settings" in p for p in report["problems"])
    proc, report = _run(tmp_path, cases_file, jobs, ["--same-cases", str(other)])
    assert proc.returncode == 0 and report["parity_match"], proc.stdout + proc.stderr
    assert report["case_list_files"]["run_2"] == str(other) and report["case_list_files"]["run_1"] == str(cases_file)


@pytest.mark.parametrize("rows", [
    "t1 ty24 t1\nt2 ty24 t2\n",                              # a row missing
    "t3 ty24 t3\nt2 ty25 t2\nt1 ty24 t1\n",                 # a column changed
    "t3 ty24 t3\nt2 ty24 t2\nt1 ty24 t1\nt1 ty24 t1\n",    # a row duplicated
    "t3 ty24 t3\nt2 ty24 t2\nt1 ty24 t1\nt4 ty24 t4\n",    # a row added
])
def test_a_same_cases_file_with_other_cases_is_rejected(tmp_path, rows):
    cases_file, jobs = _setup(tmp_path, [FULL, FULL], [FULL, FULL])
    _reordered_run(tmp_path, cases_file)
    wrong = tmp_path / "wrong.txt"
    wrong.write_text(rows)
    proc, report = _run(tmp_path, cases_file, jobs, ["--same-cases", str(wrong)])
    assert proc.returncode == 1 and report is None
    assert "not the same cases" in proc.stdout + proc.stderr


def test_a_run_with_an_unnamed_reordering_is_still_rejected(tmp_path):
    cases_file, jobs = _setup(tmp_path, [FULL, FULL], [FULL, FULL])
    named = _reordered_run(tmp_path, cases_file, run="run_2")
    lines = [l for l in cases_file.read_text().splitlines() if l and not l.startswith("#")]
    third = tmp_path / "third.txt"
    third.write_text("# another order\n" + "\n".join(lines[1:] + lines[:1]) + "\n")
    settings = tmp_path / "orig" / "run_1" / "run.settings"
    old = settings.read_text().split("cases_sha256=")[1].split("\n")[0]
    settings.write_text(settings.read_text().replace(old, hashlib.sha256(third.read_bytes()).hexdigest()))
    proc, report = _run(tmp_path, cases_file, jobs, ["--same-cases", str(named)])
    assert proc.returncode == 1 and not report["parity_match"]
    assert any("run_1" in p and "different case list" in p for p in report["problems"])
    assert not any("different settings" in p for p in report["problems"])


def test_a_strict_miss_alone_is_no_match(tmp_path):
    # Same by-line and lenient results on both sides, but the strict ranges do not touch.
    orig = {"t1": (3, 3), "t2": (1, 3, 1), "t3": (3, 3)}
    harb = {"t1": (2, 3, 1), "t2": (2, 3), "t3": (3, 3)}
    cases_file, jobs = _setup(tmp_path, [orig, orig], [harb, harb])
    proc, report = _run(tmp_path, cases_file, jobs)
    assert report["overlap"] == {"strict": False, "by_line": True, "lenient_strict": True, "lenient_by_line": True}
    assert proc.returncode == 3 and not report["parity_match"], proc.stdout + proc.stderr
