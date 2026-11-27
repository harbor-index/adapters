"""Collect parity scores from both sides and check the overlap criterion.

Original side: one folder per run, ``run_<N>/<task_id>/evaluation_result_<level>_1.md``
written by upstream (``run_original.sh``), plus that run's ``status.tsv`` and
``run.settings``.
Harbor side: one Harbor job directory per run; each trial's ``result.json``,
``verifier/score.json`` (written by ``tests/score.py``) and the agent's
``agent/request_meta.json``. When cases without a valid score were attempted
again in a second job, give both directories joined by a comma
(``jobs/full-run1,jobs/full-run1-retry``), the run's own job first and
the retry jobs after it in the order they were made: a later directory only
fills cases that have no valid score yet. The first valid attempt of a case
counts; a later attempt of the same case is listed and ignored.

From the repository root:

    python3 src/taxcalcbench/parity/collect_parity.py \\
        --cases src/taxcalcbench/parity/full_cases.txt \\
        --same-cases src/taxcalcbench/parity/full_cases_harbor_order.txt \\
        --original parity-experiments/full-original/run_{1,2,3,4,5} \\
        --harbor jobs/full-run1,jobs/full-run1-retry jobs/full-run2 \\
                 jobs/full-run3,jobs/full-run3-retry,jobs/full-run3-retry2 \\
                 jobs/full-run4,jobs/full-run4-retry,jobs/full-run4-retry2 jobs/full-run5 \\
        [--json parity-experiments/summary.json] \\
        [--export-dir parity-experiments/results_collection]

Metrics per run, all four as upstream's evaluator computes them, over the cases
with a valid score: strictly correct returns (%), mean correct-by-line (%), and
their lenient versions, where a line within $5 of the expected amount counts
(lenient correct returns (%), mean correct-by-line lenient (%)).
Both sides use the same arithmetic: a case's by-line score is
100 * (correct lines / scored lines), and a run's score is the mean over its
scored cases in case-list order. The original side's lenient count is read from
the expected and actual amounts on each line of upstream's report and checked
against the lenient summary upstream printed; the Harbor side's from the
verifier's per-line ``lenient_correct`` marks, checked against its
``lenient_correct_return``.

What counts as a valid score:
- original: the launcher recorded the case as ``ok`` (its own status file for
  the case) and upstream's evaluation file is there. The line counts are read
  from the per-line marks in that file, because the percentage upstream prints
  is rounded to two decimals;
- Harbor: the verifier wrote a score for the answer file it found, the agent's
  own record of the call is complete and says the reply arrived (``outcome``
  ``ok``), and the answer file Harbor collected from the task container is
  byte for byte the reply the agent recorded. With that evidence the score
  belongs to the model's reply, so the trial counts even when a later
  container or network step raised an error (it is listed as a warning).
  A trial without a recorded error also needs Harbor's ``reward_valid``.
  Every other trial (API failure, time limit, verifier error, lost answer) is
  listed with its reason and left out, as upstream leaves out a failed
  generation.

The data are complete when each side has the same number of runs (at least 2);
every run covers exactly the case list given with --cases (original runs record
the hash of their case list) and scored every case; every run has a complete
record of its settings; all runs of a side used the same settings; both sides
used the same upstream commit, model, thinking level, reasoning effort, API
base URL and LiteLLM / OpenAI SDK versions, in every attempt that left a complete
request record, counted or failed; the Harbor trials used one Harbor version,
one agent version, request parameters (reasoning, streaming, timeout, PDF
count) that are the same in every attempt of a case that recorded any of them and fit
the tax year in the counted trials (``ty25`` rows of the case list: a streamed
request with at least one PDF; ``ty24`` rows: no PDF) and, for every attempt
of a task, the same task files; the job configuration names an agent and a
model and agrees with the request records; every job and trial has an id,
every trial names the job folder it is in, a job folder holds one trial per
task, and the job folders of a run are given in the order they were made
(by the local start time Harbor recorded for each job);
--level equals the runs' level; each case has the same number of scored lines
everywhere; no answer exceeded the scorer's size limit; no two served-model
names were reported; and no job, trial or run is given twice. The report lists
every attempt, counted or not.
Scores are exact fractions, so equal results compare equal. Parity is claimed
only when the data are complete and the run ranges overlap on all four metrics.
Exit code: 0 = complete and matching, 1 = incomplete (also when the collection
stops with a message and writes no report, for example for a run made with
LIMIT or an unreadable file), 3 = complete but the ranges do not overlap. A
wrong command line ends with exit code 2 before anything is read or removed.

Formulas (docs/adapters.mdx): sample SEM = sqrt(sum((x - mean)^2) / (n (n - 1)));
the sides match when max(A) >= min(B) and max(B) >= min(A).

Each call first removes the --json file and the result files in --export-dir,
so a collection that stops later leaves no earlier report behind.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from fractions import Fraction
from pathlib import Path

CORRECT_MARK = ": ✓ correct, expected:"
INCORRECT_MARK = ": ✗ incorrect, expected:"
LINE_RESULT = re.compile(r": (✓ correct|✗ incorrect), expected: (\S+), actual: (\S+)[ \t]*$", re.M)
LENIENT_DOLLARS = 5  # upstream: abs(actual - expected) <= 5
METRICS = ("strict", "by_line", "lenient_strict", "lenient_by_line")
LABELS = {"strict": "strict", "by_line": "by-line", "lenient_strict": "lenient strict",
          "lenient_by_line": "lenient by-line"}
PROVENANCE = ("upstream_commit", "model", "thinking_level", "reasoning_effort", "api_base", "litellm", "openai")
REQUEST_KEYS = ("reasoning", "stream", "timeout", "n_pdfs")
# The answer file as Harbor collects it from the task container (task.toml: artifacts).
GRADED_ANSWER = Path("artifacts") / "app" / "output" / "return.txt"
AGENT_REPLY = Path("agent") / "return.txt"


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SystemExit(f"{path}: not readable as UTF-8 text ({exc})") from exc


def _json(path: Path) -> dict:
    try:
        data = json.loads(_text(path))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path}: not valid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: expected a JSON object")
    return data


def _cases(path: Path) -> dict[str, str]:
    """task id -> tax year ("ty25" / "ty24", or "" when the list has no second column), in list order."""
    rows = [l.split() for l in _text(path).splitlines() if l.strip() and not l.startswith("#")]
    return {r[0]: (r[1] if len(r) > 1 else "") for r in rows}


def _case_rows(path: Path) -> list[tuple]:
    """The case list's lines (comments and blank lines left out), sorted, so two orders of one list compare equal."""
    return sorted(tuple(l.split()) for l in _text(path).splitlines() if l.strip() and not l.startswith("#"))


def _comparable(settings: dict) -> dict:
    """A run's settings without the case-list hash, which the case-list check compares on its own."""
    return {k: v for k, v in settings.items() if k != "cases_sha256"}


def _record_provenance(meta: dict) -> tuple:
    """(commit, model, level, effort, API base URL, litellm, openai) as the agent recorded them for one request."""
    effort = (meta.get("reasoning") or {}).get("effort") if isinstance(meta.get("reasoning"), dict) else None
    return (meta.get("upstream_commit"), meta.get("model"), meta.get("thinking_level"), effort, meta.get("api_base"),
            meta.get("litellm_version"), meta.get("openai_version"))


def _case_score(correct: int, total: int, lenient: int) -> dict:
    """Same arithmetic for both sides: counts only, turned into exact fractions later.
    lenient: lines within $5 of the expected amount (a correct line always counts)."""
    return {"strict": correct == total, "correct": correct, "total": total,
            "lenient_strict": lenient == total, "lenient": lenient}


def _original_run(run_dir: Path, cases: dict[str, str], level: str) -> dict:
    status = {}
    status_file = run_dir / "status.tsv"
    if status_file.is_file():
        rows = [line.split("\t") for line in _text(status_file).splitlines()]
        if rows and "task_id" in rows[0] and "status" in rows[0]:
            i_task, i_status = rows[0].index("task_id"), rows[0].index("status")
            status = {r[i_task]: r[i_status] for r in rows[1:] if len(r) == len(rows[0])}
    settings_file = run_dir / "run.settings"
    settings, cases_sha = {}, None
    if settings_file.is_file():
        settings = dict(line.split("=", 1) for line in _text(settings_file).splitlines() if "=" in line)
        cases_sha = settings.get("cases_sha256")
        if settings.pop("limit", "0") != "0":
            raise SystemExit(f"{run_dir}: this run used LIMIT, so it is not a full run of the case list")
    scored, unscored, attempts = {}, {}, []
    for task_id in cases:
        case_dir = run_dir / task_id
        f = case_dir / f"evaluation_result_{level}_1.md"
        # The launcher's own record for the case: "ok" only after upstream's call
        # ended and both files were taken over. An attempt that was cut off has
        # another status, whatever files it left.
        case_status_file = case_dir / "status"
        fields = _text(case_status_file).rstrip("\n").split("\t") if case_status_file.is_file() else []
        case_status = fields[5] if len(fields) == 6 else None
        earlier = sorted(d.name for d in case_dir.glob("attempt_*") if d.is_dir()) if case_dir.is_dir() else []
        if case_dir.is_dir():
            attempts.append({"task": task_id, "status": case_status, "attempt": fields[1] if len(fields) == 6 else None,
                             "earlier_attempts": earlier})
        if case_status != "ok" or not f.is_file():
            unscored[task_id] = (f"status {case_status}" if case_status and case_status != "ok"
                                 else f"no evaluation file for level {level}" if case_status == "ok"
                                 else status.get(task_id, "no evaluation file") if not f.is_file()
                                 else "no status record of the launcher for this case")
            continue
        text = _text(f)
        # Per-line results are the block before upstream's summary; nothing after it is counted.
        block = text.split("\nStrictly correct return:", 1)[0]
        correct = block.count(CORRECT_MARK)
        total = correct + block.count(INCORRECT_MARK)
        strict = re.search(r"Strictly correct return: (True|False)", text)
        reported = re.search(r"Correct \(by line\): ([\d.]+)%", text)
        lenient_strict = re.search(r"Lenient correct return: (True|False)", text)
        lenient_reported = re.search(r"Correct \(by line, lenient\): ([\d.]+)%", text)
        if not total or not strict or not reported or not lenient_strict or not lenient_reported:
            raise SystemExit(f"{f}: not a complete upstream evaluation report")
        marks = LINE_RESULT.findall(block)
        if len(marks) != total:
            raise SystemExit(f"{f}: the expected and actual amounts of some lines cannot be read")
        lenient = sum(1 for mark, expected, actual in marks
                      if mark.startswith("✓") or abs(float(actual) - float(expected)) <= LENIENT_DOLLARS)
        score = _case_score(correct, total, lenient)
        if abs(100 * correct / total - float(reported.group(1))) > 0.0051 or (strict.group(1) == "True") != score["strict"]:
            raise SystemExit(f"{f}: per-line marks disagree with the summary upstream printed")
        if (abs(100 * lenient / total - float(lenient_reported.group(1))) > 0.0051
                or (lenient_strict.group(1) == "True") != score["lenient_strict"]):
            raise SystemExit(f"{f}: lenient per-line results disagree with the lenient summary upstream printed")
        scored[task_id] = score
    shared = set()
    provenance = tuple(settings.get(key) for key in PROVENANCE)
    if all(provenance):
        shared.add(provenance)
    seen_all = set(shared)
    # A run is identified by the id its launcher wrote once, when the run folder
    # was made (a copy keeps it, a rerun does not change it); older folders
    # without one by their records.
    fingerprint = hashlib.sha256()
    for name in (("run.id",) if (run_dir / "run.id").is_file() else ("run.settings", "run.meta", "status.tsv")):
        if (run_dir / name).is_file():
            fingerprint.update((run_dir / name).read_bytes())
    extra = sorted(p.name for p in run_dir.iterdir() if p.is_dir() and p.name not in cases)
    return {"name": run_dir.name, "scored": scored, "unscored": unscored, "warnings": [], "notes": [],
            "settings": settings, "served_models": [], "shared": shared, "seen_all": seen_all, "checksums": {},
            "params": {}, "params_all": {}, "extra": extra, "ids": [fingerprint.hexdigest()], "cases_sha256": cases_sha,
            "attempts": attempts}


def _harbor_trial(trial: Path, result: dict) -> tuple[dict | None, str, dict]:
    """(score, reason it is not counted, the agent's request record) for one trial."""
    exc = (result.get("exception_info") or {}).get("exception_type")
    meta_file = trial / "agent" / "request_meta.json"
    meta = _json(meta_file) if meta_file.is_file() else {}
    score_file = trial / "verifier" / "score.json"
    if not score_file.is_file():
        return None, exc or "no verifier score", meta
    s = _json(score_file)
    complete = all(_record_provenance(meta)) and all(key in meta for key in ("stream", "timeout", "n_pdfs"))
    if meta.get("outcome") != "ok" or not complete:
        # Without the agent's own complete record that a reply arrived there is
        # no evidence of what was sent and scored.
        return None, exc or "no complete request record (agent/request_meta.json)", meta
    if s.get("output_found") is not True:
        return None, "answer written by the agent did not reach the verifier", meta
    graded, reply = trial / GRADED_ANSWER, trial / AGENT_REPLY
    if not graded.is_file() or not reply.is_file() or graded.read_bytes() != reply.read_bytes():
        return None, "the collected answer file is not the reply the agent recorded", meta
    if not exc and result.get("reward_valid") is not True:
        return None, "reward marked invalid", meta
    lines = s.get("lines")
    if not lines:
        raise SystemExit(f"{score_file}: no per-line results")
    if not all(isinstance(line.get("correct"), bool) for line in lines):
        raise SystemExit(f"{score_file}: per-line results are not true/false")
    correct = sum(1 for line in lines if line["correct"])
    if (correct == len(lines)) != bool(s.get("strictly_correct_return")):
        raise SystemExit(f"{score_file}: per-line results disagree with strictly_correct_return")
    if not all(isinstance(line.get("lenient_correct"), bool) for line in lines):
        raise SystemExit(f"{score_file}: per-line lenient results are not true/false")
    if any(line["correct"] and not line["lenient_correct"] for line in lines):
        raise SystemExit(f"{score_file}: a correct line is marked not lenient-correct")
    for key in ("strictly_correct_return", "lenient_correct_return"):
        if not isinstance(s.get(key), bool):
            raise SystemExit(f"{score_file}: {key} is not true/false")
    for line in lines:
        e, a = line.get("expected"), line.get("actual")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (e, a)):
            raise SystemExit(f"{score_file}: a line has no numeric expected/actual amount")
        # The verifier's rule, which is upstream's: exact equality, and abs(actual - expected) <= 5.
        if line["correct"] != (a == e) or line["lenient_correct"] != (a == e or abs(a - e) <= LENIENT_DOLLARS):
            raise SystemExit(f"{score_file}: per-line marks disagree with the line's own amounts")
    lenient = sum(1 for line in lines if line["lenient_correct"])
    for key, n in (("correct_by_line_score", correct), ("lenient_correct_by_line_score", lenient)):
        if not isinstance(s.get(key), (int, float)) or isinstance(s.get(key), bool) or abs(s[key] - n / len(lines)) > 1e-9:
            raise SystemExit(f"{score_file}: {key} disagrees with the per-line results")
    score = _case_score(correct, len(lines), lenient)
    if score["lenient_strict"] != s["lenient_correct_return"]:
        raise SystemExit(f"{score_file}: lenient per-line results disagree with lenient_correct_return")
    score["output_too_large"] = bool(s.get("output_too_large"))
    return score, "", meta


def _harbor_job(job_dir: Path, cases: dict[str, str]) -> dict:
    """Valid scores, failures and agent settings of one Harbor job directory."""
    job = {"scored": {}, "failed": {}, "warnings": {}, "notes": [], "settings": {}, "served": set(), "shared": set(),
           "seen_all": set(), "checksums": {}, "params": {}, "params_all": {}, "extra": [], "trial_ids": [],
           "agents": set(), "harbor_versions": set(), "attempts": [], "started_at": None}
    seen: set[str] = set()
    job_result = job_dir / "result.json"
    job_record = _json(job_result) if job_result.is_file() else {}
    job["job_id"], job["started_at"] = job_record.get("id"), job_record.get("started_at")
    config_file = job_dir / "config.json"
    if config_file.is_file():
        agents = _json(config_file).get("agents") or [{}]
        kwargs = agents[0].get("kwargs") or {}
        job["settings"] = {"agent": agents[0].get("import_path") or agents[0].get("name"),
                           "model": agents[0].get("model_name"),
                           "kwargs": json.dumps(kwargs, sort_keys=True)}
        configured = (agents[0].get("model_name"), kwargs.get("thinking_level", "high"))
    else:
        configured = (None, None)
    for trial in sorted(p for p in job_dir.iterdir() if p.is_dir()):
        result_file = trial / "result.json"
        if not result_file.is_file():
            continue
        result = _json(result_file)
        if not result.get("task_name"):
            raise SystemExit(f"{result_file}: no task_name; not a Harbor trial result")
        # Trial folder names are truncated; result.json has the full task name
        # ("column-tax/taxcalcbench__<task_id>").
        task_id = result["task_name"].rsplit("__", 1)[-1]
        job["trial_ids"].append(result.get("id"))
        trial_job = (result.get("config") or {}).get("job_id")
        if job["job_id"] and trial_job != job["job_id"]:
            job["notes"].append(f"{task_id}: the trial names job {trial_job}, not the job folder it is in"
                                if trial_job else f"{task_id}: the trial's result.json names no job")
        if task_id not in cases:
            job["extra"].append(task_id)
            continue
        if task_id in seen:
            raise SystemExit(f"{job_dir}: more than one trial for {task_id}; use one trial per task per job")
        seen.add(task_id)
        # Every attempt of a task, counted or not, must have run on the same task files.
        job["checksums"].setdefault(task_id, set()).add(result.get("task_checksum"))
        score, why, meta = _harbor_trial(trial, result)
        exc = (result.get("exception_info") or {}).get("exception_type")
        job["attempts"].append({"job": job_dir.name, "task": task_id, "counted": score is not None,
                                "reason": why or None, "exception": exc})
        # Settings and versions of every attempt that left a record, counted or failed.
        if all(_record_provenance(meta)):
            job["seen_all"].add(_record_provenance(meta))
        if meta:
            job["agents"].add(meta.get("agent"))
            job["harbor_versions"].add(meta.get("harbor_version"))
        # The request parameters of every attempt that recorded any of them, counted
        # or failed; a parameter the record lacks is compared as "not recorded".
        if any(key in meta for key in REQUEST_KEYS):
            params = json.dumps({key: meta.get(key, "(not recorded)") for key in REQUEST_KEYS}, sort_keys=True)
            job["params_all"].setdefault(task_id, set()).add(params)
        if score is None:
            job["failed"][task_id] = why
            continue
        notices = job["warnings"].setdefault(task_id, [])
        if exc:
            notices.append(f"counted; the reply was delivered and scored, and the trial also recorded {exc}")
        year = cases.get(task_id, "")
        fits = {"ty25": meta.get("stream") is True and isinstance(meta.get("n_pdfs"), int) and meta["n_pdfs"] >= 1,
                "ty24": meta.get("n_pdfs") == 0}.get(year, True)
        if not fits:
            job["notes"].append(f"{task_id}: request parameters do not fit a {year} case "
                                f"(stream {meta.get('stream')!r}, {meta.get('n_pdfs')!r} PDFs)")
        if score.pop("output_too_large"):
            # The scorer treats such an answer as empty; upstream has no limit, so the two sides would differ.
            job["notes"].append(f"{task_id}: answer over the scorer's size limit, scored as an empty answer")
        if meta.get("stream") is False and meta.get("response_status") not in (None, "completed"):
            notices.append(f"reply status {meta.get('response_status')!r}, not 'completed'")
        if meta.get("stream") and meta.get("stream_completed") is False:
            notices.append("streamed reply ended without response.completed")
        if (meta.get("model"), meta.get("thinking_level")) != configured:
            job["notes"].append(f"{task_id}: the job configuration says {configured}, the request record says "
                                f"{(meta.get('model'), meta.get('thinking_level'))} (use upstream's canonical names)")
        job["scored"][task_id] = score
        if meta.get("served_model"):
            job["served"].add(meta["served_model"])
        job["params"][task_id] = params
        job["shared"].add(_record_provenance(meta))
    return job


def _harbor_run(job_dirs: list[Path], cases: dict[str, str]) -> dict:
    """One run = a job directory, optionally followed by retry jobs for its failed trials."""
    run = {"scored": {}, "warnings": [], "notes": [], "served": set(), "shared": set(), "seen_all": set(),
           "checksums": {}, "params": {}, "params_all": {}, "extra": [], "ids": [], "agents": set(),
           "harbor_versions": set(), "attempts": []}
    failed, settings, previous_start = {}, None, None
    for job_dir in job_dirs:
        job = _harbor_job(job_dir, cases)
        # "First valid attempt" means first in time, so the folders must come in the order
        # they were made (Harbor's own start time of each job, as its result.json has it).
        if len(job_dirs) > 1 and not job["started_at"]:
            run["notes"].append(f"{job_dir.name}: no started_at (result.json of the job folder), so the order "
                                "of this run's job folders cannot be checked")
        if previous_start and job["started_at"] and job["started_at"] < previous_start[1]:
            run["notes"].append(f"{job_dir.name} was started before {previous_start[0]}; give a run's job folders "
                                "in the order they were made")
        if job["started_at"]:
            previous_start = (job_dir.name, job["started_at"])
        if job["job_id"] is None:
            run["notes"].append(f"{job_dir.name}: no job id (result.json of the job folder)")
        run["ids"].append(f"job:{job['job_id'] or job_dir.resolve()}")
        for trial_id in job["trial_ids"]:
            if trial_id is None:
                run["notes"].append(f"{job_dir.name}: a trial has no id in its result.json")
            else:
                run["ids"].append(f"trial:{trial_id}")
        for task_id in sorted(set(job["scored"]) & set(run["scored"])):
            # The first valid attempt counts, so a retry can never replace a score.
            del job["scored"][task_id]
            job["params"].pop(task_id, None)
            job["warnings"].pop(task_id, None)
            for attempt in job["attempts"]:
                if attempt["task"] == task_id and attempt["counted"]:
                    attempt.update(counted=False, reason="an earlier attempt of this case already counts")
            run["warnings"].append(f"{task_id}: the attempt in {job_dir.name} is ignored; an earlier attempt of "
                                   "this case already counts")
        if settings is None:
            settings = job["settings"]
        elif job["settings"] != settings:
            raise SystemExit(f"{job_dir}: agent settings differ from the first job of this run "
                             f"({job['settings']} vs {settings})")
        for key in ("served", "shared", "seen_all", "agents", "harbor_versions"):
            run[key] |= job[key]
        for key in ("checksums", "params_all"):
            for task_id, values in job[key].items():
                run[key].setdefault(task_id, set()).update(values)
        run["warnings"] += [f"{task_id}: {text} ({job_dir.name})" for task_id, texts in job["warnings"].items()
                            for text in texts]
        run["scored"].update(job["scored"])
        run["params"].update(job["params"])
        failed.update(job["failed"])
        for key in ("notes", "extra", "attempts"):
            run[key] += job[key]
    run.update(name=",".join(d.name for d in job_dirs), settings=settings or {},
               unscored={t: failed.get(t, "no trial") for t in cases if t not in run["scored"]},
               served_models=sorted(run["served"]), extra=sorted(set(run["extra"])))
    return run


def _mean_sem(xs: list) -> tuple[float, float | None]:
    """Mean and sample SEM = sqrt(sum((x - mean)^2) / (n (n - 1)))."""
    n = len(xs)
    m = sum(xs, Fraction(0)) / n
    if n < 2:
        return float(m), None
    return float(m), math.sqrt(sum(((x - m) ** 2 for x in xs), Fraction(0)) / (n * (n - 1)))


def _overlap(a: list, b: list) -> bool:
    return max(a) >= min(b) and max(b) >= min(a)


def _run_scores(run: dict, cases: dict[str, str]) -> dict[str, Fraction] | None:
    """Exact run scores in percent for METRICS, so equal results compare equal on both sides;
    None without a scored case."""
    vals = [run["scored"][t] for t in cases if t in run["scored"]]
    if not vals:
        return None
    for v in vals:
        if not (isinstance(v["correct"], int) and isinstance(v["total"], int) and isinstance(v["lenient"], int)
                and 0 <= v["correct"] <= v["lenient"] <= v["total"] and v["total"] > 0):
            raise SystemExit(f"run {run['name']}: invalid line counts {v}")
    n = len(vals)
    return {"strict": Fraction(100 * sum(1 for v in vals if v["strict"]), n),
            "by_line": sum((Fraction(100 * v["correct"], v["total"]) for v in vals), Fraction(0)) / n,
            "lenient_strict": Fraction(100 * sum(1 for v in vals if v["lenient_strict"]), n),
            "lenient_by_line": sum((Fraction(100 * v["lenient"], v["total"]) for v in vals), Fraction(0)) / n}


def _summ(runs: list[dict], cases: dict[str, str]) -> dict:
    scores = [_run_scores(run, cases) for run in runs]
    have = [s for s in scores if s is not None]
    out = {"all_runs_scored": len(have) == len(scores)}
    for m in METRICS:
        xs = [s[m] for s in have]
        out[f"{m}_exact"] = xs
        out[f"{m}_runs"] = [float(s[m]) if s else None for s in scores]
        out[m] = _mean_sem(xs) if have else None
    return out


def _fmt(pair: tuple[float, float | None] | None) -> str:
    if pair is None:
        return "no scored run"
    m, e = pair
    return f"{m:.2f} ± {e:.4f}" if e is not None else f"{m:.2f} (one run, no SEM)"


def _round(values: list) -> list:
    return [None if x is None else round(x, 4) for x in values]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--original", type=Path, nargs="+", required=True,
                   help="one run folder per run (run_original.sh's run_<N>)")
    p.add_argument("--harbor", nargs="+", required=True,
                   help="one Harbor job dir per run; add a run's retry jobs after it, joined by commas")
    p.add_argument("--cases", type=Path, default=Path(__file__).parent / "full_cases.txt")
    p.add_argument("--same-cases", type=Path, nargs="+", default=[],
                   help="case list files with the same rows as --cases in another order (comments, blank lines and spacing may differ); "
                        "an original run recorded with one of them counts as a run of --cases")
    p.add_argument("--level", default="high")
    p.add_argument("--json", type=Path, help="write the full summary here")
    p.add_argument("--export-dir", type=Path,
                   help="write result_{original,harbor}_trial<N>.json (one per run) here")
    args = p.parse_args()

    # An aborted collection must not leave an earlier report or export behind:
    # everything removable goes first, then the paths are judged.
    wrong_paths = []
    if args.json and args.json.is_dir():
        wrong_paths.append(f"--json is a folder: {args.json}")
    elif args.json and args.json.exists():
        args.json.unlink()
    if args.export_dir and args.export_dir.is_dir():
        for stale in args.export_dir.glob("result_*_trial*.json"):
            stale.unlink()
    elif args.export_dir and args.export_dir.exists():
        wrong_paths.append(f"--export-dir is not a folder: {args.export_dir}")
    if wrong_paths:
        raise SystemExit("; ".join(wrong_paths))
    if not args.cases.is_file():
        raise SystemExit(f"case list not found: {args.cases}")
    listed = [l.split()[0] for l in _text(args.cases).splitlines() if l.strip() and not l.startswith("#")]
    cases = _cases(args.cases)
    if not cases or len(cases) != len(listed):
        raise SystemExit(f"{args.cases}: empty case list or duplicate task ids")
    cases_sha = hashlib.sha256(args.cases.read_bytes()).hexdigest()
    # Reordered copies of the case list: same lines, so a run made with one ran the same cases.
    allowed_lists = {cases_sha: str(args.cases)}
    for alt in args.same_cases:
        if not alt.is_file():
            raise SystemExit(f"case list not found: {alt}")
        if _case_rows(alt) != _case_rows(args.cases):
            raise SystemExit(f"{alt}: not the same cases as {args.cases}")
        allowed_lists.setdefault(hashlib.sha256(alt.read_bytes()).hexdigest(), str(alt))
    harbor_groups = [[Path(d) for d in group.split(",") if d] for group in args.harbor]
    all_dirs = [d.resolve() for group in harbor_groups for d in group] + [d.resolve() for d in args.original]
    if len(set(all_dirs)) != len(all_dirs):
        raise SystemExit("the same run directory was given more than once")
    for d in all_dirs:
        if not d.is_dir():
            raise SystemExit(f"not a directory: {d}")
    orig_runs = [_original_run(d, cases, args.level) for d in args.original]
    harb_runs = [_harbor_run(group, cases) for group in harbor_groups]
    o, h = _summ(orig_runs, cases), _summ(harb_runs, cases)

    problems, warnings = [], []
    if len(orig_runs) < 2 or len(harb_runs) < 2:
        problems.append("fewer than 2 runs on a side")
    if len(orig_runs) != len(harb_runs):
        problems.append(f"unequal run counts (original {len(orig_runs)}, Harbor {len(harb_runs)})")
    for side, runs in (("original", orig_runs), ("harbor", harb_runs)):
        if any(_comparable(r["settings"]) != _comparable(runs[0]["settings"]) for r in runs):
            problems.append(f"{side} runs used different settings: {[r['settings'] for r in runs]}")
        if any(not r["settings"] or not r["shared"] for r in runs):
            problems.append(f"{side}: a run has no complete record of its settings "
                            "(original: run.settings; Harbor: config.json and request_meta.json)")
        for run in runs:
            if not run["scored"]:
                problems.append(f"{side} {run['name']}: no scored case")
            if run["unscored"]:
                detail = ", ".join(f"{t} ({why})" for t, why in run["unscored"].items())
                problems.append(f"{side} {run['name']}: {len(run['unscored'])} of {len(cases)} cases not scored: {detail}")
            problems += [f"{side} {run['name']}: {note}" for note in run["notes"]]
            warnings += [f"{side} {run['name']}: {w}" for w in run["warnings"]]
    served = sorted({m for r in harb_runs for m in r["served_models"]})
    if len(served) > 1:
        problems.append(f"the API reported more than one model name on the Harbor side: {served}")
    for label, key in (("agent version", "agents"), ("Harbor version", "harbor_versions")):
        values = {v for r in harb_runs for v in r[key]}
        if len(values) > 1 or None in values:
            problems.append(f"Harbor trials were made with more than one {label} (or recorded none): "
                            f"{sorted(map(str, values))}")
    if any(not r["settings"].get("agent") or not r["settings"].get("model") for r in harb_runs):
        problems.append("a Harbor job's config.json names no agent or no model")
    # The runs must be distinct, cover exactly this case list, and use one task revision.
    ids = [i for r in orig_runs + harb_runs for i in r["ids"]]
    if len(set(ids)) != len(ids):
        problems.append("the same run, job or trial was given more than once (same job id, trial id or run records)")
    for run in orig_runs:
        if run["cases_sha256"] not in allowed_lists:
            problems.append(f"original {run['name']} was run with a different case list file than --cases and the --same-cases files "
                            "(the hash covers every byte of the file; a sanity run is not a parity run)")
    for side, runs in (("original", orig_runs), ("harbor", harb_runs)):
        for run in runs:
            if run["extra"]:
                problems.append(f"{side} {run['name']} contains cases outside the case list: {', '.join(run['extra'][:5])}")
    for task_id in cases:
        sums = set().union(*(r["checksums"].get(task_id, set()) for r in harb_runs))
        if len(sums) > 1 or None in sums:
            problems.append(f"Harbor trials of {task_id} ran on different task files (or recorded no task checksum)")
        if len(set().union(*(r["params_all"].get(task_id, set()) for r in harb_runs))) > 1:
            problems.append(f"Harbor trials of {task_id} were sent with different request parameters")
        totals = {r["scored"][task_id]["total"] for r in orig_runs + harb_runs if task_id in r["scored"]}
        if len(totals) > 1:
            problems.append(f"{task_id}: the number of scored lines differs between runs or sides ({sorted(totals)})")
    efforts = {json.loads(params)["reasoning"] and json.dumps(json.loads(params)["reasoning"], sort_keys=True)
               for r in harb_runs for params in r["params"].values()}
    if len(efforts) > 1 or None in efforts:
        problems.append(f"Harbor requests used more than one reasoning setting (or recorded none): {sorted(map(str, efforts))}")
    levels = {r["settings"].get("thinking_level") for r in orig_runs if r["settings"]}
    if levels and levels != {args.level}:
        problems.append(f"--level {args.level} differs from the original runs' thinking level {sorted(map(str, levels))}")
    # Both sides must have used the same upstream commit, model, level, effort, API base URL and client versions.
    side_shared = {side: set().union(*(r["seen_all"] for r in runs))
                   for side, runs in (("original", orig_runs), ("harbor", harb_runs))}
    if not side_shared["original"] or not side_shared["harbor"]:
        problems.append("cannot compare settings across sides: a side has no complete record of "
                        "commit / model / level / effort / API base URL / LiteLLM and OpenAI SDK versions")
    elif side_shared["original"] != side_shared["harbor"] or len(side_shared["original"]) != 1:
        problems.append(f"the two sides did not use the same {PROVENANCE}: "
                        f"original {sorted(map(str, side_shared['original']))}, harbor {sorted(map(str, side_shared['harbor']))}")

    exact = {m: (o.pop(f"{m}_exact"), h.pop(f"{m}_exact")) for m in METRICS}
    comparable = o.pop("all_runs_scored") and h.pop("all_runs_scored") and bool(exact["strict"][0]) and bool(exact["strict"][1])
    overlap = {m: (_overlap(*exact[m]) if comparable else None) for m in METRICS}
    complete = not problems
    match = bool(complete and all(overlap[m] for m in METRICS))
    def run_record(side: str, i: int, run: dict, summary: dict) -> dict:
        return {"side": side, "trial": i + 1, "run": run["name"], "settings": run["settings"],
                "n_cases": len(cases), "n_scored": len(run["scored"]),
                "strictly_correct_returns_pct": summary["strict_runs"][i],
                "correct_by_line_pct": summary["by_line_runs"][i],
                "lenient_correct_returns_pct": summary["lenient_strict_runs"][i],
                "correct_by_line_lenient_pct": summary["lenient_by_line_runs"][i],
                "unscored": run["unscored"],
                "served_models": run["served_models"],
                "provenance": [dict(zip(PROVENANCE, t)) for t in sorted(run["shared"], key=str)],
                "task_checksums": {t: sorted(map(str, sums)) for t, sums in run["checksums"].items()},
                "attempts": run["attempts"],
                "per_task": {t: run["scored"].get(t) for t in cases}}

    # The files first, the verdict last: a report that cannot be written gives no verdict.
    try:
        if args.export_dir:
            args.export_dir.mkdir(parents=True, exist_ok=True)
            for side, runs, summary in (("original", orig_runs, o), ("harbor", harb_runs, h)):
                for i, run in enumerate(runs):
                    (args.export_dir / f"result_{side}_trial{i + 1}.json").write_text(
                        json.dumps(run_record(side, i, run, summary), indent=2, allow_nan=False) + "\n", encoding="utf-8")
        if args.json:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(json.dumps({
                "complete": complete, "parity_match": match, "problems": problems, "warnings": warnings,
                "case_list_files": {r["name"]: allowed_lists.get(r["cases_sha256"]) for r in orig_runs},
                "overlap": overlap, "n_cases": len(cases), "original": o, "harbor": h,
                "served_models_harbor": served,
                "runs": {"original": [run_record("original", i, r, o) for i, r in enumerate(orig_runs)],
                         "harbor": [run_record("harbor", i, r, h) for i, r in enumerate(harb_runs)]},
            }, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"cannot write the report ({exc}); no verdict given") from exc
    for name, s, runs in (("original", o, orig_runs), ("harbor", h, harb_runs)):
        print(f"{name:9s} " + " | ".join(f"{LABELS[m]} {_fmt(s[m])}" for m in METRICS))
        print(f"{'':9s} per run: " + "  ".join(f"{LABELS[m]} {_round(s[f'{m}_runs'])}" for m in METRICS)
              + f"  scored {[len(r['scored']) for r in runs]} of {len(cases)}")
    print(f"range overlap{'' if complete else ' (provisional)'}: "
          + ", ".join(f"{LABELS[m]} {overlap[m]}" for m in METRICS))
    if served:
        print("model reported by the API (Harbor side):", ", ".join(served))
    for w in warnings:
        print("warning:", w)
    if match:
        print("PARITY MATCH: data complete and the run ranges overlap on all four metrics")
    elif complete:
        print("NO MATCH: data complete, but the run ranges do not overlap on every metric")
    else:
        print("INCOMPLETE, no parity claim possible:")
        for prob in problems:
            print("  -", prob)

    sys.exit(0 if match else (3 if complete else 1))


if __name__ == "__main__":
    try:
        main()
    except (OSError, UnicodeDecodeError, ValueError, KeyError, TypeError, AttributeError) as exc:
        # A file or folder that cannot be read as expected: a message, exit code 1, no report.
        sys.exit(f"collection stopped: {type(exc).__name__}: {exc}")
