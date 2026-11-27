"""parity/run_original.sh on a fake upstream checkout with a fake `uv`.

No model, no network: the fake `uv` writes (or fails to write) the two files
upstream's CLI would save, so the launcher's bookkeeping can be tested.
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "parity" / "run_original.sh"

# The launcher calls: uv run --locked --project <checkout> python -c <code> MODEL LEVEL
#                 and: uv run --locked --project <checkout> python -m tax_calc_bench.main --provider openai --model M ...
FAKE_UV = r'''#!/bin/bash
mode=$(cat "$FAKE_MODE_FILE")
[ "$1" = run ] && [ "$2" = --locked ] && [ "$3" = --project ] && [ "$5" = python ] || { echo "unexpected uv call: $*" >&2; exit 64; }
echo "$4" > "$FAKE_PROJECT_FILE"
if [ "$6" = -c ]; then
  find . -path './tax_calc_bench/*/results/*' -type f | wc -l | tr -d ' ' > "$FAKE_SAVED_OUTPUTS_FILE"
  if [ "$mode" = slow_start ]; then echo "$$" > "$FAKE_PID_FILE.start"; sleep 3; fi   # uv building the environment
  shasum -a 256 tax_calc_bench/main.py | cut -d' ' -f1 > "$FAKE_PID_FILE.export_sha"   # the exported code, as Python would read it
  if [ "$mode" = dirty_at_startup ]; then echo "x = 3" > "$4/tax_calc_bench/main.py"; fi
  if [ "$mode" = bad_locale ]; then echo "upstream would read its test data as ascii" >&2; exit 1; fi
  model=$8; [ "$model" = luna-alias ] && model=gpt-6-luna
  echo "NAMES $model $9 ${FAKE_EFFORT:-high}"
  echo "VERSIONS ${FAKE_LITELLM:-1.100.5} 2.43.0 3.13.9"
  if [ "$mode" = wrong_module ]; then echo "MODULE /somewhere/else/tax_calc_bench"; else echo "MODULE $(pwd -P)/tax_calc_bench"; fi
  exit 0
fi
while [ $# -gt 0 ]; do
  case "$1" in
    --model) model=$2; shift ;;
    --thinking-level) level=$2; shift ;;
    --tax-year) year=$2; shift ;;
    --test-name) name=$2; shift ;;
  esac
  shift
done
echo "$name" >> "$FAKE_CALLS"
call=$(wc -l < "$FAKE_CALLS" | tr -d ' ')
echo "$$" > "$FAKE_PID_FILE"
dir="tax_calc_bench/$year/results/$name/openai/$model"
mkdir -p "$dir"
save() {
  echo "reply for $name (call $call)$1" > "$dir/model_completed_return_${level}_1.md"
  printf 'evaluation for %s\nStrictly correct return: True\n' "$name" > "$dir/evaluation_result_${level}_1.md"
}
case "$mode:$name" in
  fail:*|fail_c2:c2) echo "simulated API error"; exit 0 ;;
  slow_c1:c1) sleep 3 ;;
  hang_c2:c2) sleep 60 & echo $! > "$FAKE_PID_FILE.child"; wait ;;   # a call that never returns by itself
  deaf_c2:c2) trap '' TERM; sleep 60 & echo $! > "$FAKE_PID_FILE.child"; wait ;;   # ... and ignores TERM
  saved_then_hang_c2:c2) save; sleep 60 & wait ;;      # upstream saved its files, then the run is stopped
  straggler_c1:c1) save; ( sleep 1; d2="tax_calc_bench/ty25/results/c2/openai/$model"; mkdir -p "$d2";
                           echo "written by a leftover of c1" > "$d2/model_completed_return_${level}_1.md";
                           printf 'x\nStrictly correct return: True\n' > "$d2/evaluation_result_${level}_1.md" ) &
                   exit 0 ;;
  straggler_c1:c2) sleep 2.5; echo "simulated API error"; exit 0 ;;
  kill_c2:c2) save; kill -9 $PPID; sleep 1.5; save " written by the orphan"; exit 0 ;;
  empty_eval_c2:c2) save; : > "$dir/evaluation_result_${level}_1.md"; exit 0 ;;
  empty_out_c2:c2) save; : > "$dir/model_completed_return_${level}_1.md"; exit 0 ;;
  no_verdict_c2:c2) save; echo "half a report" > "$dir/evaluation_result_${level}_1.md"; exit 0 ;;
  exit7_c2:c2) echo "simulated crash"; exit 7 ;;
esac
save
'''
GIT = ("git", "-c", "user.name=t", "-c", "user.email=t@example.com")


@pytest.fixture()
def env(tmp_path):
    repo = tmp_path / "upstream"
    (repo / "tax_calc_bench").mkdir(parents=True)
    (repo / "tax_calc_bench" / "main.py").write_text("x = 1\n")
    (repo / ".gitignore").write_text(".env\n.venv/\n")
    # Like upstream, the checkout has a saved leaderboard output committed in c2's slot.
    slot = repo / "tax_calc_bench" / "ty25" / "results" / "c2" / "openai" / "gpt-6-luna"
    slot.mkdir(parents=True)
    (slot / "model_completed_return_high_1.md").write_text("committed leaderboard reply\n")
    (slot / "evaluation_result_high_1.md").write_text("committed evaluation\nStrictly correct return: False\n")
    run = lambda *a: subprocess.run(a, cwd=repo, check=True, capture_output=True, text=True)  # noqa: E731
    run("git", "init", "-q")
    run(*GIT, "add", ".")
    run(*GIT, "commit", "-q", "-m", "init")
    head = run("git", "rev-parse", "HEAD").stdout.strip()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(FAKE_UV)
    uv.chmod(uv.stat().st_mode | stat.S_IEXEC)
    cases = tmp_path / "cases.txt"
    cases.write_text("# task_id tax_year upstream_case\nc1 ty25 c1\nc2 ty25 c2\nc3 ty24 c3\n")
    mode = tmp_path / "mode"
    mode.write_text("ok")
    work_parent = tmp_path / "tmp"
    work_parent.mkdir()
    e = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "TAXCALCBENCH_ROOT": str(repo),
         "TAXCALCBENCH_EXPECTED_REF": head, "OPENAI_API_KEY": "sk-dummy-local", "OUT_DIR": str(tmp_path / "out"),
         "CASES_FILE": str(cases), "FAKE_MODE_FILE": str(mode), "FAKE_CALLS": str(tmp_path / "calls"),
         "FAKE_PID_FILE": str(tmp_path / "fake.pid"), "FAKE_PROJECT_FILE": str(tmp_path / "project"),
         "FAKE_SAVED_OUTPUTS_FILE": str(tmp_path / "saved_outputs"),
         "OPENAI_BASE_URL": "http://127.0.0.1:9/v1", "TMPDIR": str(work_parent)}
    for name in ("OPENAI_API_BASE", "MODEL", "THINKING_LEVEL", "LIMIT", "RERUN_FAILED", "ALLOW_DOTENV", "FAKE_LITELLM",
                 "FAKE_EFFORT"):
        e.pop(name, None)
    return e, tmp_path, mode, repo


def _default_signals():
    # Back to their defaults, so the launcher can trap them whatever started the tests (nohup, a background shell).
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT, signal.SIGUSR1):
        signal.signal(signum, signal.SIG_DFL)


def _launch(e, **extra):
    return subprocess.run(["bash", str(SCRIPT)], env={**e, **extra}, capture_output=True, text=True,
                          preexec_fn=_default_signals)


def _start(e, **extra):
    return subprocess.Popen(["bash", str(SCRIPT)], env={**e, **extra}, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, preexec_fn=_default_signals)


def _status(tmp_path, run="1"):
    rows = (tmp_path / "out" / f"run_{run}" / "status.tsv").read_text().splitlines()[1:]
    return {r.split("\t")[0]: r.split("\t") for r in rows}


def _calls(tmp_path):
    f = tmp_path / "calls"
    return f.read_text().split() if f.exists() else []


def _wait_for(condition, seconds=15):
    deadline = time.time() + seconds
    while not condition():
        assert time.time() < deadline, "timed out waiting"
        time.sleep(0.05)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _reply(tmp_path, task="c2", run="1"):
    return (tmp_path / "out" / f"run_{run}" / task / "model_completed_return_high_1.md").read_text()


def test_complete_run_leaves_the_checkout_alone(env):
    e, tmp_path, mode, repo = env
    proc = _launch(e, RUN="1")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert {k: v[5] for k, v in _status(tmp_path).items()} == {"c1": "ok", "c2": "ok", "c3": "ok"}
    settings = (tmp_path / "out" / "run_1" / "run.settings").read_text().splitlines()
    assert settings[:7] == [f"upstream_commit={e['TAXCALCBENCH_EXPECTED_REF']}", "model=openai/gpt-6-luna",
                            "thinking_level=high", "reasoning_effort=high", "api_base=http://127.0.0.1:9/v1",
                            "litellm=1.100.5", "openai=2.43.0"]
    assert settings[7].startswith("cases_sha256=") and settings[8] == "limit=0" and len(settings) == 9
    # c2's result is this run's reply, never the output the checkout has committed.
    assert _reply(tmp_path) == "reply for c2 (call 2)\n"
    # Upstream ran in a private export with the checkout's uv project: the checkout is untouched
    # (its committed output is still there) and the export is gone.
    assert (tmp_path / "project").read_text().strip() == str(repo)
    assert (tmp_path / "saved_outputs").read_text().strip() == "0"  # the export holds no saved output
    assert subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=repo,
                          capture_output=True, text=True).stdout == ""
    assert list((tmp_path / "tmp").iterdir()) == []
    again = _launch(e, RUN="1")
    assert again.returncode == 1 and "refusing to reuse" in again.stderr


def test_failed_case_exits_2_and_rerun_keeps_the_first_attempt(env):
    e, tmp_path, mode, _ = env
    mode.write_text("fail_c2")
    proc = _launch(e, RUN="1")
    assert proc.returncode == 2
    assert _status(tmp_path)["c2"][5] == "generation_failed"
    mode.write_text("ok")
    changed = _launch(e, RUN="1", RERUN_FAILED="1", MODEL="other-model")
    assert changed.returncode == 1 and "settings differ" in changed.stderr
    other_env = _launch(e, RUN="1", RERUN_FAILED="1", FAKE_LITELLM="1.99.0")
    assert other_env.returncode == 1 and "settings differ" in other_env.stderr
    other_effort = _launch(e, RUN="1", RERUN_FAILED="1", FAKE_EFFORT="medium")
    assert other_effort.returncode == 1 and "settings differ" in other_effort.stderr
    other_endpoint = _launch(e, RUN="1", RERUN_FAILED="1", OPENAI_BASE_URL="http://127.0.0.1:4000/proxy-b/v1")
    assert other_endpoint.returncode == 1 and "settings differ" in other_endpoint.stderr  # same host, other port and path
    limited = _launch(e, RUN="1", RERUN_FAILED="1", LIMIT="1")
    assert limited.returncode == 1 and "settings differ" in limited.stderr
    run_id = (tmp_path / "out" / "run_1" / "run.id").read_text()
    cases = tmp_path / "cases.txt"
    original_list = cases.read_text()
    cases.write_text(original_list + "c4 ty24 c4\n")
    edited = _launch(e, RUN="1", RERUN_FAILED="1")
    assert edited.returncode == 1 and "settings differ" in edited.stderr
    cases.write_text(original_list)
    proc = _launch(e, RUN="1", RERUN_FAILED="1")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    status = _status(tmp_path)
    assert status["c2"][1] == "2" and status["c2"][5] == "ok" and status["c1"][1] == "1"
    first = tmp_path / "out" / "run_1" / "c2" / "attempt_1"
    assert "simulated API error" in (first / "run.log").read_text()
    # c1 and c3 were not called again
    assert _calls(tmp_path) == ["c1", "c2", "c3", "c2"]
    # The run keeps the id it got when its folder was made.
    assert run_id.strip() and (tmp_path / "out" / "run_1" / "run.id").read_text() == run_id


def test_killed_launcher_never_leaves_a_result_and_its_orphan_cannot_supply_one(env):
    # The launcher is killed right after upstream saved c2's files. Nothing was
    # recorded for c2, so it is run again; the killed launcher's upstream process
    # lives on for a moment and writes once more, into its own private folder.
    e, tmp_path, mode, _ = env
    mode.write_text("kill_c2")
    proc = _launch(e, RUN="1")
    assert proc.returncode != 0
    assert not (tmp_path / "out" / "run_1" / "c2" / "model_completed_return_high_1.md").exists()
    mode.write_text("ok")
    proc = _launch(e, RUN="1", RERUN_FAILED="1")  # at once: the dead launcher holds no lock
    assert proc.returncode == 0, proc.stdout + proc.stderr
    status = _status(tmp_path)
    assert status["c2"][1] == "2" and {v[5] for v in status.values()} == {"ok"}
    assert _calls(tmp_path) == ["c1", "c2", "c2", "c3"]
    time.sleep(2)  # the orphan has written by now
    assert _reply(tmp_path) == "reply for c2 (call 3)\n"
    assert (tmp_path / "out" / "run_1" / "c2" / "attempt_1" / "status").read_text().split("\t")[5].strip() == "in_progress"


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT])
def test_stopping_the_launcher_stops_upstream_at_once(env, signum):
    e, tmp_path, mode, _ = env
    mode.write_text("hang_c2")
    proc = _start(e, RUN="1")
    _wait_for(lambda: _calls(tmp_path) == ["c1", "c2"] and (tmp_path / "fake.pid.child").exists())
    time.sleep(0.3)
    upstream_pid = int((tmp_path / "fake.pid").read_text())
    worker_pid = int((tmp_path / "fake.pid.child").read_text())  # a process upstream's call started
    assert _alive(upstream_pid) and _alive(worker_pid)
    started = time.time()
    proc.send_signal(signum)
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 130 and "interrupted" in err
    assert time.time() - started < 8  # not after the 60 s call
    _wait_for(lambda: not _alive(upstream_pid) and not _alive(worker_pid), seconds=5)
    assert _calls(tmp_path) == ["c1", "c2"]  # c3 was never started
    status = _status(tmp_path)  # the ledger is written on the way out
    assert status["c1"][5] == "ok" and status["c2"][5] == "in_progress"
    assert list((tmp_path / "tmp").iterdir()) == []
    mode.write_text("ok")
    proc = _launch(e, RUN="1", RERUN_FAILED="1")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    status = _status(tmp_path)
    assert status["c2"][1] == "2" and {v[5] for v in status.values()} == {"ok"}


def test_an_upstream_call_that_ignores_term_is_killed_after_the_grace_period(env):
    e, tmp_path, mode, _ = env
    mode.write_text("deaf_c2")
    proc = _start(e, RUN="1")
    _wait_for(lambda: (tmp_path / "fake.pid.child").exists())
    time.sleep(0.3)
    pids = [int((tmp_path / name).read_text()) for name in ("fake.pid", "fake.pid.child")]
    started = time.time()
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=40)
    assert proc.returncode == 130 and 8 < time.time() - started < 20
    _wait_for(lambda: not any(_alive(pid) for pid in pids), seconds=5)
    assert _status(tmp_path)["c2"][5] == "in_progress"


def test_a_second_signal_does_not_restart_the_stop(env):
    e, tmp_path, mode, _ = env
    mode.write_text("deaf_c2")
    proc = _start(e, RUN="1")
    _wait_for(lambda: (tmp_path / "fake.pid.child").exists())
    time.sleep(0.3)
    pids = [int((tmp_path / name).read_text()) for name in ("fake.pid", "fake.pid.child")]
    started = time.time()
    proc.send_signal(signal.SIGTERM)
    time.sleep(6)
    proc.send_signal(signal.SIGINT)
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 130 and err.count("interrupted") == 1
    assert time.time() - started < 13.5  # one 10 s grace period from the first signal, not a new one from the second
    _wait_for(lambda: not any(_alive(pid) for pid in pids), seconds=5)
    assert _status(tmp_path)["c2"][5] == "in_progress" and list((tmp_path / "tmp").iterdir()) == []


def test_a_stop_during_start_up_waits_for_that_step_and_records_nothing(env):
    e, tmp_path, mode, _ = env
    mode.write_text("slow_start")
    proc = _start(e, RUN="1")
    _wait_for(lambda: (tmp_path / "fake.pid.start").exists())
    proc.send_signal(signal.SIGTERM)
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 130 and "interrupted during start-up" in err
    assert not (tmp_path / "out" / "run_1").exists() and _calls(tmp_path) == []
    assert list((tmp_path / "tmp").iterdir()) == []


def test_a_launcher_killed_during_start_up_holds_no_lock(env):
    e, tmp_path, mode, _ = env
    mode.write_text("slow_start")
    proc = _start(e, RUN="1")
    _wait_for(lambda: (tmp_path / "fake.pid.start").exists())
    helper = int((tmp_path / "fake.pid.start").read_text())
    proc.kill()
    proc.wait(timeout=10)
    proc.stdout.close()
    proc.stderr.close()
    assert _alive(helper)  # the start-up step outlives the launcher for a moment
    mode.write_text("ok")
    again = _launch(e, RUN="1")  # at once: the start-up step was started without the lock
    assert again.returncode == 0, again.stdout + again.stderr
    _wait_for(lambda: not _alive(helper), seconds=10)


def test_stop_works_when_nobody_reads_the_launchers_output(env):
    # The terminal (or the pipe the output went to) is gone: the stop must still
    # end upstream's call and write the ledger.
    e, tmp_path, mode, _ = env
    mode.write_text("hang_c2")
    proc = _start(e, RUN="1")
    _wait_for(lambda: (tmp_path / "fake.pid.child").exists())
    time.sleep(0.3)
    pids = [int((tmp_path / name).read_text()) for name in ("fake.pid", "fake.pid.child")]
    proc.stdout.close()
    proc.stderr.close()
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=30) == 130
    _wait_for(lambda: not any(_alive(pid) for pid in pids), seconds=5)
    assert _status(tmp_path)["c2"][5] == "in_progress"


def test_a_closed_reader_of_the_progress_lines_does_not_end_the_run(env):
    e, tmp_path, mode, _ = env
    mode.write_text("slow_c1")
    proc = _start(e, RUN="1")
    _wait_for(lambda: _calls(tmp_path) != [])
    proc.stdout.close()  # like `... | head -1` once head has left
    assert proc.wait(timeout=60) == 0
    assert {v[5] for v in _status(tmp_path).values()} == {"ok"}


def test_a_tool_that_warns_after_the_reader_left_does_not_end_the_run(env):
    # Nobody reads the launcher's stderr (closed terminal or pipe) and git prints a warning
    # there on every call. The warning is lost; git itself must keep working.
    import shutil

    e, tmp_path, mode, _ = env
    real_git = shutil.which("git")
    shim = tmp_path / "bin" / "git"
    shim.write_text(f'#!/bin/bash\necho "warning: something git wants to say" >&2\nexec {real_git} "$@"\n')
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    reader, writer = os.pipe()
    os.close(reader)
    try:
        proc = subprocess.run(["bash", str(SCRIPT)], env={**e, "RUN": "1"}, stdout=subprocess.PIPE, stderr=writer,
                              text=True, preexec_fn=_default_signals)
    finally:
        os.close(writer)
    assert proc.returncode == 0, proc.stdout
    assert {v[5] for v in _status(tmp_path).values()} == {"ok"}


def test_a_signal_the_launcher_does_not_handle_still_stops_upstream(env):
    # SIGUSR1 ends the shell. On its way out the launcher stops upstream's call and removes
    # the export; the case stays in_progress and can be run again.
    e, tmp_path, mode, _ = env
    mode.write_text("hang_c2")
    proc = _start(e, RUN="1")
    _wait_for(lambda: _calls(tmp_path) == ["c1", "c2"] and (tmp_path / "fake.pid.child").exists())
    time.sleep(0.3)
    pids = [int((tmp_path / name).read_text()) for name in ("fake.pid", "fake.pid.child")]
    try:
        assert all(_alive(pid) for pid in pids)
        proc.send_signal(signal.SIGUSR1)
        proc.communicate(timeout=30)
        assert proc.returncode == -signal.SIGUSR1
        _wait_for(lambda: not any(_alive(pid) for pid in pids), seconds=5)
    finally:
        for pid in pids:
            if _alive(pid):
                os.kill(pid, signal.SIGKILL)
    assert list((tmp_path / "tmp").iterdir()) == []
    assert (tmp_path / "out" / "run_1" / "c2" / "status").read_text().split("\t")[5].strip() == "in_progress"
    mode.write_text("ok")
    again = _launch(e, RUN="1", RERUN_FAILED="1")
    assert again.returncode == 0, again.stdout + again.stderr
    status = _status(tmp_path)
    assert status["c2"][1] == "2" and {v[5] for v in status.values()} == {"ok"}


def test_upstream_is_stopped_even_when_its_pid_was_not_noted_yet(tmp_path):
    # A signal can arrive between the start of upstream's call and the line that notes its
    # pid. That moment is too short to hit from outside, so the launcher's own stop function
    # is run on exactly that state: a call started the launcher's way, `child` still empty.
    import re

    stop_child = re.search(r"^stop_child\(\) \{\n.*?^\}\n", SCRIPT.read_text(), flags=re.M | re.S).group(0)
    harness = tmp_path / "harness.sh"
    harness.write_text('child=""\n' + stop_child
                       + "python3 -c 'import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' sleep 60"
                       + " < /dev/null > /dev/null 2>&1 &\n"
                       + 'echo $! > "$1"\nsleep 0.5\nstop_child\n')
    pid_file = tmp_path / "pid"
    started = time.time()
    proc = subprocess.run(["bash", str(harness), str(pid_file)], capture_output=True, text=True,
                          preexec_fn=_default_signals)
    pid = int(pid_file.read_text())
    try:
        assert proc.returncode == 0, proc.stderr
        assert not _alive(pid) and time.time() - started < 8
    finally:
        if _alive(pid):
            os.kill(pid, signal.SIGKILL)


def test_files_saved_by_a_stopped_attempt_are_kept_and_never_counted(env):
    # Upstream had saved both files when the run was stopped. They stay with that
    # attempt (status in_progress), and the rerun makes a new call for the case.
    e, tmp_path, mode, _ = env
    mode.write_text("saved_then_hang_c2")
    proc = _start(e, RUN="1")
    _wait_for(lambda: _calls(tmp_path) == ["c1", "c2"] and (tmp_path / "fake.pid").exists())
    time.sleep(0.5)
    proc.send_signal(signal.SIGTERM)
    proc.communicate(timeout=30)
    assert proc.returncode == 130
    dest = tmp_path / "out" / "run_1" / "c2"
    assert _status(tmp_path)["c2"][5] == "in_progress"
    assert (dest / "model_completed_return_high_1.md").read_text() == "reply for c2 (call 2)\n"
    mode.write_text("ok")
    assert _launch(e, RUN="1", RERUN_FAILED="1").returncode == 0
    assert _status(tmp_path)["c2"][1] == "2" and _reply(tmp_path) == "reply for c2 (call 3)\n"
    assert (dest / "attempt_1" / "model_completed_return_high_1.md").read_text() == "reply for c2 (call 2)\n"
    assert (dest / "attempt_1" / "status").read_text().split("\t")[5].strip() == "in_progress"


def test_a_leftover_process_of_one_case_cannot_supply_the_next(env):
    # c1's call leaves a process behind that would write c2's files a second later;
    # c2's own call then fails. Nothing may make c2 look finished.
    e, tmp_path, mode, _ = env
    mode.write_text("straggler_c1")
    proc = _launch(e, RUN="1")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    status = _status(tmp_path)
    assert status["c1"][5] == "ok" and status["c2"][5] == "generation_failed"
    assert not (tmp_path / "out" / "run_1" / "c2" / "model_completed_return_high_1.md").exists()


def test_paths_with_spaces(env):
    e, tmp_path, mode, _ = env
    spaced = tmp_path / "temp folder"
    spaced.mkdir()
    proc = _launch(e, RUN="1", TMPDIR=str(spaced), OUT_DIR=str(tmp_path / "out dir"))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    rows = (tmp_path / "out dir" / "run_1" / "status.tsv").read_text().splitlines()[1:]
    assert [r.split("\t")[5] for r in rows] == ["ok", "ok", "ok"] and list(spaced.iterdir()) == []


def test_one_launcher_per_run_folder(env):
    e, tmp_path, mode, _ = env
    mode.write_text("slow_c1")
    first = _start(e, RUN="1")
    try:
        _wait_for(lambda: _calls(tmp_path) != [])
        second = _launch(e, RUN="1", RERUN_FAILED="1")
        assert second.returncode == 1 and "another run_original.sh" in second.stderr
        other = _launch(e, RUN="2")  # another run label is another folder
        assert other.returncode == 0, other.stdout + other.stderr
    finally:
        out, err = first.communicate(timeout=60)
    assert first.returncode == 0, out + err
    assert {v[5] for v in _status(tmp_path).values()} == {"ok"}
    assert {v[5] for v in _status(tmp_path, "2").values()} == {"ok"}


def test_empty_evaluation_is_not_ok_and_can_be_rerun(env):
    e, tmp_path, mode, _ = env
    mode.write_text("empty_eval_c2")
    assert _launch(e, RUN="1").returncode == 2
    assert _status(tmp_path)["c2"][5] == "evaluation_missing"
    mode.write_text("ok")
    assert _launch(e, RUN="1", RERUN_FAILED="1").returncode == 0
    assert _status(tmp_path)["c2"][5] == "ok"


def test_empty_reply_and_report_without_a_verdict_are_not_ok(env):
    e, tmp_path, mode, _ = env
    mode.write_text("empty_out_c2")
    assert _launch(e, RUN="1").returncode == 2
    assert _status(tmp_path)["c2"] == ["c2", "1", "0", "no", "yes", "generation_failed"]
    mode.write_text("no_verdict_c2")
    assert _launch(e, RUN="2").returncode == 2
    assert _status(tmp_path, "2")["c2"] == ["c2", "1", "0", "yes", "no", "evaluation_missing"]


def test_upstream_exit_code_and_run_times_are_recorded(env):
    e, tmp_path, mode, _ = env
    mode.write_text("exit7_c2")
    assert _launch(e, RUN="1").returncode == 2
    status = _status(tmp_path)
    assert status["c2"][2] == "7" and status["c2"][5] == "generation_failed" and status["c1"][2] == "0"
    meta = (tmp_path / "out" / "run_1" / "run.meta").read_text()
    assert "started: " in meta and "finished: " in meta and "complete: 2/3 cases ok" in meta


def test_rerun_after_a_rerun_that_was_stopped_while_filing_the_earlier_attempt(env):
    e, tmp_path, mode, _ = env
    mode.write_text("fail_c2")
    assert _launch(e, RUN="1").returncode == 2
    mode.write_text("ok")
    # The rerun gets SIGTERM right after it moved c2's status into attempt_1/.
    trigger = _mv_shim(tmp_path, "*/c2/status")
    stopped = _launch(e, RUN="1", RERUN_FAILED="1", FAKE_MV_TRIGGER=str(trigger))
    dest = tmp_path / "out" / "run_1" / "c2"
    assert stopped.returncode == 130, stopped.stdout + stopped.stderr
    assert not (dest / "status").exists() and (dest / "attempt_1" / "status").exists()
    assert _calls(tmp_path) == ["c1", "c2", "c3"]  # no new call was made
    assert _status(tmp_path)["c2"] == ["c2", "", "", "", "", "in_progress"]  # the ledger still shows the case
    proc = _launch(e, RUN="1", RERUN_FAILED="1", FAKE_MV_TRIGGER=str(trigger))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    status = _status(tmp_path)
    assert status["c2"][5] == "ok" and status["c2"][1] == "2"  # one earlier attempt on file, so this is the second
    assert sorted(p.name for p in dest.glob("attempt_*")) == ["attempt_1"]
    assert "simulated API error" in (dest / "attempt_1" / "run.log").read_text()


def _mv_shim(tmp_path, trigger_on: str) -> Path:
    """A `mv` on PATH that sends TERM to the launcher right after it moved the file named by ``trigger_on``."""
    shim = tmp_path / "bin" / "mv"
    shim.write_text('#!/bin/bash\n/bin/mv "$@"; rc=$?\n'
                    f'if [ -f "$FAKE_MV_TRIGGER" ]; then case "$1" in {trigger_on}) rm -f "$FAKE_MV_TRIGGER"; kill -TERM "$PPID" ;; esac; fi\n'
                    'exit $rc\n')
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    trigger = tmp_path / "trigger"
    trigger.write_text("x")
    return trigger


def test_a_stop_in_the_middle_of_filing_keeps_one_attempt_in_one_folder(env):
    # The rerun is stopped when it has moved only the first file of the earlier attempt.
    # The next rerun files the rest into the same attempt_1/ and runs attempt 2.
    e, tmp_path, mode, _ = env
    mode.write_text("fail_c2")
    assert _launch(e, RUN="1").returncode == 2
    mode.write_text("ok")
    trigger = _mv_shim(tmp_path, "*/c2/run.log")
    stopped = _launch(e, RUN="1", RERUN_FAILED="1", FAKE_MV_TRIGGER=str(trigger))
    dest = tmp_path / "out" / "run_1" / "c2"
    assert stopped.returncode == 130, stopped.stdout + stopped.stderr
    assert (dest / "status").exists() and sorted(p.name for p in (dest / "attempt_1").iterdir()) == ["run.log"]
    proc = _launch(e, RUN="1", RERUN_FAILED="1", FAKE_MV_TRIGGER=str(trigger))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    status = _status(tmp_path)
    assert status["c2"][5] == "ok" and status["c2"][1] == "2"
    assert sorted(p.name for p in dest.glob("attempt_*")) == ["attempt_1"]
    assert sorted(p.name for p in (dest / "attempt_1").iterdir()) == ["run.log", "status"]
    assert (dest / "attempt_1" / "status").read_text().split("\t")[5].strip() == "generation_failed"
    assert _calls(tmp_path) == ["c1", "c2", "c3", "c2"]  # one new call, for the second attempt


def test_export_holds_the_commits_bytes_whatever_the_users_git_config_says(env):
    import hashlib

    e, tmp_path, mode, repo = env
    config = tmp_path / "gitconfig"
    config.write_text("[core]\n\tautocrlf = true\n")  # would turn LF into CRLF on checkout
    proc = _launch(e, RUN="1", GIT_CONFIG_GLOBAL=str(config))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    committed = subprocess.run(["git", "show", "HEAD:tax_calc_bench/main.py"], cwd=repo, check=True, capture_output=True).stdout
    assert (tmp_path / "fake.pid.export_sha").read_text().strip() == hashlib.sha256(committed).hexdigest()


@pytest.mark.parametrize("slow_check, rerun", [(2, {}), (3, {"RERUN_FAILED": "1"})])
def test_a_launcher_killed_during_a_checkout_check_holds_no_lock(env, slow_check, rerun):
    import shutil

    e, tmp_path, mode, _ = env
    real_git = shutil.which("git")
    shim = tmp_path / "bin" / "git"
    # `git status` runs three times: before the lock is taken, right after start-up (2) and at
    # the end of the run (3). The two made while the lock is held take 3 s here, one per test.
    shim.write_text('#!/bin/bash\ncase " $* " in *" status "*)\n'
                    '  n=$(( $(cat "$FAKE_GIT_COUNT" 2>/dev/null || echo 0) + 1 )); echo $n > "$FAKE_GIT_COUNT"\n'
                    '  if [ "$n" = "$FAKE_GIT_SLOW_CALL" ] && [ -f "$FAKE_GIT_SLOW" ]; then rm -f "$FAKE_GIT_SLOW"; echo $$ > "$FAKE_PID_FILE.git"; sleep 3; fi ;;\nesac\n'
                    f'exec {real_git} "$@"\n')
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    slow = tmp_path / "git-slow"
    slow.write_text("x")
    extra = {"FAKE_GIT_COUNT": str(tmp_path / "git-count"), "FAKE_GIT_SLOW": str(slow), "FAKE_GIT_SLOW_CALL": str(slow_check)}
    proc = _start(e, RUN="1", **extra)
    _wait_for(lambda: (tmp_path / "fake.pid.git").exists())
    helper = int((tmp_path / "fake.pid.git").read_text())
    proc.kill()
    proc.wait(timeout=10)
    proc.stdout.close()
    proc.stderr.close()
    assert _alive(helper)  # the check outlives the launcher for a moment
    (tmp_path / "git-count").unlink()
    again = _launch(e, RUN="1", **extra, **rerun)  # at once: the check was started without the lock
    assert again.returncode == 0, again.stdout + again.stderr
    assert {v[5] for v in _status(tmp_path).values()} == {"ok"}
    _wait_for(lambda: not _alive(helper), seconds=10)


def test_a_launcher_killed_during_the_export_holds_no_lock(env):
    import shutil

    e, tmp_path, mode, _ = env
    real_git = shutil.which("git")
    shim = tmp_path / "bin" / "git"
    # The export is made while the lock is held. Its last step (git checkout-index) takes 3 s here, once.
    shim.write_text('#!/bin/bash\ncase " $* " in *" checkout-index "*)\n'
                    '  if [ -f "$FAKE_GIT_SLOW" ]; then rm -f "$FAKE_GIT_SLOW"; echo $$ > "$FAKE_PID_FILE.git"; sleep 3; fi ;;\nesac\n'
                    f'exec {real_git} "$@"\n')
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    slow = tmp_path / "git-slow"
    slow.write_text("x")
    pid_file = tmp_path / "fake.pid.git"
    proc = _start(e, RUN="1", FAKE_GIT_SLOW=str(slow))
    _wait_for(lambda: pid_file.exists() and pid_file.read_text().strip() != "")
    helper = int(pid_file.read_text())
    proc.kill()
    proc.wait(timeout=10)
    proc.stdout.close()
    proc.stderr.close()
    assert _alive(helper)  # the export step outlives the launcher for a moment
    again = _launch(e, RUN="1", FAKE_GIT_SLOW=str(slow))  # at once: the export was started without the lock
    assert again.returncode == 0, again.stdout + again.stderr
    assert {v[5] for v in _status(tmp_path).values()} == {"ok"}
    _wait_for(lambda: not _alive(helper), seconds=10)


@pytest.mark.parametrize("failing_step", ["read-tree", "ls-files", "checkout-index"])
def test_a_failed_export_step_stops_the_start(env, failing_step):
    # One git step of the export reports an error (checkout-index after it has written its
    # files). Nothing may run on such an export.
    import shutil

    e, tmp_path, mode, _ = env
    real_git = shutil.which("git")
    shim = tmp_path / "bin" / "git"
    shim.write_text('#!/bin/bash\ncase " $* " in *" $FAKE_GIT_FAIL "*)\n'
                    f'  if [ "$FAKE_GIT_FAIL" = checkout-index ]; then {real_git} "$@"; fi\n'
                    '  echo "simulated git failure" >&2; exit 1 ;;\nesac\n'
                    f'exec {real_git} "$@"\n')
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    proc = _launch(e, RUN="1", FAKE_GIT_FAIL=failing_step)
    assert proc.returncode == 1 and "could not export" in proc.stderr, proc.stdout + proc.stderr
    assert not (tmp_path / "out" / "run_1").exists() and _calls(tmp_path) == []
    assert list((tmp_path / "tmp").iterdir()) == []


def test_a_grep_error_during_the_export_stops_the_start(env):
    # The export's file list goes through grep. Here grep passes the whole list on and then
    # reports an error (exit 2). "Nothing to print" (exit 1) is fine; an error is a failed export.
    import shutil

    e, tmp_path, mode, _ = env
    real_grep = shutil.which("grep")
    shim = tmp_path / "bin" / "grep"
    shim.write_text(f'#!/bin/bash\nif [ "$1" = -zv ]; then {real_grep} "$@"; echo "simulated grep error" >&2; exit 2; fi\n'
                    f'exec {real_grep} "$@"\n')
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    proc = _launch(e, RUN="1")
    assert proc.returncode == 1 and "could not export" in proc.stderr, proc.stdout + proc.stderr
    assert not (tmp_path / "out" / "run_1").exists() and _calls(tmp_path) == []
    assert list((tmp_path / "tmp").iterdir()) == []


@pytest.mark.parametrize("state", ["status names a filed attempt", "status unusable"])
def test_files_of_an_attempt_never_go_into_a_folder_that_holds_another_one(env, state):
    # Hand-made states the launcher does not produce itself: the pending files must get the
    # next free attempt folder, so the attempt already on file is not overwritten.
    e, tmp_path, mode, _ = env
    mode.write_text("fail_c2")
    assert _launch(e, RUN="1").returncode == 2
    dest = tmp_path / "out" / "run_1" / "c2"
    (dest / "attempt_1").mkdir()
    (dest / "attempt_1" / "status").write_text("c2\t1\t0\tno\tno\tgeneration_failed\n")
    (dest / "attempt_1" / "run.log").write_text("log of the attempt already on file\n")
    if state == "status unusable":
        (dest / "status").write_text("")
    mode.write_text("ok")
    proc = _launch(e, RUN="1", RERUN_FAILED="1")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (dest / "attempt_1" / "run.log").read_text() == "log of the attempt already on file\n"
    assert "simulated API error" in (dest / "attempt_2" / "run.log").read_text()
    status = _status(tmp_path)
    assert status["c2"][5] == "ok" and status["c2"][1] == "3"


def test_a_run_folder_left_empty_by_a_stopped_launcher_is_no_run(env):
    e, tmp_path, mode, _ = env
    (tmp_path / "out" / "run_1").mkdir(parents=True)
    proc = _launch(e, RUN="1")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert {v[5] for v in _status(tmp_path).values()} == {"ok"}


def test_canonical_names_and_host_are_recorded(env):
    e, tmp_path, mode, _ = env
    proc = _launch(e, RUN="1", MODEL="luna-alias", OPENAI_BASE_URL="http://user:pw@EXAMPLE.Host:8080/v1")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    settings = (tmp_path / "out" / "run_1" / "run.settings").read_text()
    # The endpoint is recorded with port and path and without credentials.
    assert "model=openai/gpt-6-luna\n" in settings and "api_base=http://example.host:8080/v1\n" in settings
    assert "pw" not in settings


@pytest.mark.parametrize("content, message", [
    ("c1 ty25\n", "bad line"),                        # two fields: upstream would run the whole year
    ("../x ty25 c1\n", "bad line"),                   # would write outside the run folder
    ("c1 ty26 c1\n", "bad line"),
    ("c1 ty25 c1\nc1 ty25 c2\n", "listed twice"),
    ("c1 ty25 c1\nC1 ty25 c2\n", "listed twice"),      # one folder on a case-insensitive disk
    ("# only a comment\n", "no cases"),
])
def test_bad_case_lists_stop_before_anything_is_created(env, content, message):
    e, tmp_path, mode, _ = env
    (tmp_path / "cases.txt").write_text(content)
    proc = _launch(e, RUN="1")
    assert proc.returncode == 1 and message in proc.stderr
    assert not (tmp_path / "out" / "run_1").exists() and _calls(tmp_path) == []


def test_case_list_with_windows_line_ends(env):
    e, tmp_path, mode, _ = env
    (tmp_path / "cases.txt").write_text("# task_id tax_year upstream_case\r\nc1 ty25 c1\r\nc3 ty24 c3\r\n")
    proc = _launch(e, RUN="1")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _calls(tmp_path) == ["c1", "c3"] and set(_status(tmp_path)) == {"c1", "c3"}


def test_limit_runs_the_first_cases_only_and_is_recorded(env):
    e, tmp_path, mode, _ = env
    proc = _launch(e, RUN="1", LIMIT="2")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _calls(tmp_path) == ["c1", "c2"]
    assert "limit=2" in (tmp_path / "out" / "run_1" / "run.settings").read_text().splitlines()


def test_a_long_bad_case_list_still_gets_the_message(env):
    e, tmp_path, mode, _ = env
    (tmp_path / "cases.txt").write_text("".join(f"case{i} ty25\n" for i in range(4000)))
    proc = _launch(e, RUN="1")
    assert proc.returncode == 1 and "bad line" in proc.stderr and "case0 ty25" in proc.stderr
    (tmp_path / "cases.txt").write_text("".join(f"case{i} ty25 x{i}\nCASE{i} ty25 y{i}\n" for i in range(3000)))
    proc = _launch(e, RUN="1")
    assert proc.returncode == 1 and "listed twice" in proc.stderr


def test_guards(env):
    e, tmp_path, mode, repo = env
    assert _launch(e, RUN="1", LIMIT="abc").returncode == 1
    label = _launch(e, RUN="../x")
    assert label.returncode == 1 and "RUN must be" in label.stderr and not (tmp_path / "x").exists()
    assert _launch(e, RUN="1", TAXCALCBENCH_EXPECTED_REF="0" * 40).returncode == 1
    missing = _launch(e, RUN="1", CASES_FILE=str(tmp_path / "nope.txt"))
    assert missing.returncode == 1 and "case list not found" in missing.stderr
    # A .env file upstream's CLI would load from a folder above its code.
    (tmp_path / "tmp" / ".env").write_text("OPENAI_BASE_URL=http://elsewhere\n")
    dotenv = _launch(e, RUN="1")
    assert dotenv.returncode == 1 and ".env" in dotenv.stderr
    allowed = _launch(e, RUN="dotenv", ALLOW_DOTENV="1")  # allowed on request, and said so in the run's records
    assert allowed.returncode == 0 and "dotenv_allowed" in (tmp_path / "out" / "run_dotenv" / "run.meta").read_text()
    (tmp_path / "calls").unlink()
    (tmp_path / "tmp" / ".env").unlink()
    mode.write_text("bad_locale")
    locale = _launch(e, RUN="1")
    assert locale.returncode == 1 and "ascii" in locale.stderr
    mode.write_text("wrong_module")  # Python would run some other copy of upstream's code
    module = _launch(e, RUN="1")
    assert module.returncode == 1 and "not from the export" in module.stderr
    mode.write_text("dirty_at_startup")  # the start-up step itself changes a tracked file of the checkout
    changed = _launch(e, RUN="1")
    assert changed.returncode == 1 and "changed a tracked file" in changed.stderr
    subprocess.run(["git", "checkout", "-q", "--", "."], cwd=repo, check=True)
    mode.write_text("ok")
    (repo / "tax_calc_bench" / "main.py").write_text("x = 2\n")
    dirty = _launch(e, RUN="1")
    assert dirty.returncode == 1 and "local changes" in dirty.stderr
    assert not (tmp_path / "out" / "run_1").exists() and _calls(tmp_path) == []
    assert list((tmp_path / "tmp").iterdir()) == []  # no export is left behind by a refused start
