"""Verifier for one task: scores the submitted solution file.

The file is scored the way the original harness scores one generation. The
pieces map onto the upstream code as follows:

1. ``prepare_generation`` is a port, for a single generation, of the
   language-specific preprocessing in ``HumanEvalPackGenerative.process_results``
   (bigcode-evaluation-harness, ``bigcode_eval/tasks/humanevalpack.py`` lines
   392-463 at commit 8fc5bae6479c4fbbb28c3f8b644f6a15b3f3b5bd).
2. The test program is ``generation + "\\n" + reference`` with
   ``reference = "\\n" + doc["test"]`` (``code_eval_octopack.py`` line 151 and
   ``humanevalpack.py`` line 269).
3. ``execute.check_correctness`` runs it. ``execute.py`` in this directory is the
   upstream executor, vendored unmodified, so the compile and run commands and
   the timeouts are upstream's.

What this file adds around those calls:

* A submission that cannot be used (no file, not a regular file, not UTF-8
  text, larger than ``MAX_SUBMISSION_BYTES``) is scored as the empty
  generation, which is what the original pipeline scores when it has no
  generation for a problem (``humanevalpack_openai.py`` line 232).
  ``result.json`` records which case it was.
* The submitted code is untrusted. This script runs as root, but it calls the
  upstream executor in a child process that has switched to the unprivileged
  ``sandbox`` account, has its own session and a private scratch directory,
  and cannot write to the log directory or read the tests directory. The
  verdict is the exit status of that child, which is collected by this
  process and cannot be forged by the processes the child starts. Afterwards
  every process of the sandbox account is killed. Only then does this script
  write ``result.json`` and return, and the caller (``test.sh``) writes the
  reward.

The executor itself is not hardened: as in the original harness, the submitted
code runs in the same program as the unit tests, so a submission that ends the
program early with a success status, or that tampers with the executor's own
state, is reported as passed here exactly as it is there.

Exit status: 0 if the solution passed, 1 if it failed, 2 on a verifier error.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import select
import signal
import stat
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import execute

# --- Constants copied from upstream -----------------------------------------

# bigcode_eval/tasks/humanevalpack.py, lines 51-58.
LANGUAGE_TO_TIMEOUT = {
    "python": 10,
    "cpp": 60,
    "js": 10,
    "java": 10,
    "go": 20,
    "rust": 300,  # Necessary for first-time compilation of cargo
}

# bigcode_eval/tasks/humanevalpack.py, lines 71-125.
IMPORT_HELPER = {
    "python": [
        "import math",
        "import re",
        "import sys",
        "import copy",
        "import datetime",
        "import itertools",
        "import collections",
        "import heapq",
        "import statistics",
        "import functools",
        "import hashlib",
        "import numpy",
        "import numpy as np",
        "import string",
        "from typing import *",
        "from collections import *",
    ],
    "go": [
        "math",
        "strings",
        "fmt",
        "strconv",
        "time",
        "bytes",
        "regexp",
        "sort",
        "math/rand",
        "crypto/md5",
    ],
    "cpp": [
        "using namespace std;",
        "#include<stdlib.h>",
        "#include<algorithm>",
        "#include<cmath>",
        "#include<math.h>",
        "#include<numeric>",
        "#include<stdio.h>",
        "#include<vector>",
        "#include<set>",
        "#include<map>",
        "#include<queue>",
        "#include<stack>",
        "#include<list>",
        "#include<deque>",
        "#include<boost/any.hpp>",
        "#include<string>",
        "#include<climits>",
        "#include<cstring>",
        "#include<iostream>",
        "#include<sstream>",
        "#include<fstream>",
    ],
}

# code_eval_octopack.py, lines 100-111.
BASE_CARGO = """[package]
name = "rust"
version = "0.1.0"
edition = "2021"

# See more keys and their definitions at https://doc.rust-lang.org/cargo/reference/manifest.html

[dependencies]
rand = "0.4"
regex = "1"
md5 = "0.7.0"
"""

# --- Adapter-specific settings (see environment/Dockerfile) -------------------

TOOLS_ROOT = Path("/opt/toolchain")
SANDBOX_USER = "sandbox"
DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
# The upstream executor gives up after `timeout + 1` seconds. The child that
# runs it is killed this many seconds later if it is still there.
GRACE_SEC = 30
RESULT_TEXT_LIMIT = 20000
PIPE_READ_LIMIT = 1 << 20
# Far above any solution to these single-function problems (the reference
# solutions are below 4 kB); a larger file is not read into memory.
MAX_SUBMISSION_BYTES = 1 << 20

SUBMISSION_OK = "ok"
SUBMISSION_MISSING = "missing"
SUBMISSION_NOT_A_FILE = "not_a_regular_file"
SUBMISSION_NOT_UTF8 = "not_utf8"
SUBMISSION_TOO_LARGE = "too_large"


def prepare_generation(language: str, generation: str, doc: dict) -> str:
    """Apply upstream's per-language changes to one generation.

    ``language`` is the dataset config name (python, js, java, go, cpp, rust).
    The statements follow ``process_results`` line by line; the only change is
    that they act on one string instead of a list of lists.
    """
    g = generation
    if language == "python":
        python_imports = "\n".join(IMPORT_HELPER["python"])
        return (python_imports + "\n" + g).strip()
    if language == "cpp":
        cpp_imports = "\n".join(IMPORT_HELPER["cpp"])
        # Remove main in case present
        return (cpp_imports + "\n" + g.split("int main")[0]).strip()
    if language == "java":
        return g.replace("public class Main {\n    }", "").strip()
    if language == "go":
        ref = get_reference(doc)
        test_setup_str = doc["test_setup"] + "\n"
        for line in test_setup_str.split("\n"):
            line = line.replace("import", "").replace("(", "").replace(")", "").strip()
            if line.startswith('"') and line in g:
                test_setup_str = test_setup_str.replace(line, "")
        full = test_setup_str + g + "\n" + ref
        other_pkgs = set()
        for pkg in IMPORT_HELPER["go"]:
            if '"' + pkg + '"' not in full:
                p = pkg.split("/")[-1]
                # Check if the package is used
                if p + "." in full:
                    # It could appear in a comment only, and Go rejects unused
                    # imports, so require a use on a non-comment line.
                    for line in full.split("\n"):
                        if (p + "." in line) and not line.strip().startswith("//"):
                            other_pkgs.add('"' + p + '"')
                            break
        other_pkgs_str = ""
        if other_pkgs:
            # Upstream joins an unordered set; sorting only fixes the order of
            # the import lines, which Go does not care about.
            other_pkgs_str = (
                "import (\n"
                + "\n".join(["    " + p for p in sorted(other_pkgs)])
                + "\n)\n"
            )
        if ("package main" in g) and ("package main" in test_setup_str):
            g = g.replace("package main", "")
        return test_setup_str + other_pkgs_str + g
    if language == "rust":
        main = "fn main(){}\n"
        declaration = doc["declaration"]
        new_gen = ""
        if "fn main()" not in g:
            new_gen += main
        for line in declaration.split("\n"):
            if line.strip() not in g:
                # Skip if the function is already present
                if line.strip().startswith("fn") and (line.strip().split("(")[0]) in g:
                    continue
                new_gen += line.strip() + "\n"
        # If fn main() is present twice, cut off before the second one
        g = "fn main()".join(g.split("fn main()")[0:2])
        new_gen += g
        return new_gen
    if language == "js":
        return g
    raise ValueError(f"Unsupported language: {language}")


def get_reference(doc: dict) -> str:
    """``HumanEvalPack.get_reference``: ``check(func_name)`` is already included."""
    return "\n" + doc["test"]


def read_submission(path: Path) -> tuple[str, str]:
    """Return ``(generation, status)``; an unusable submission is the empty generation."""
    try:
        info = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return "", SUBMISSION_MISSING
    if not stat.S_ISREG(info.st_mode):
        return "", SUBMISSION_NOT_A_FILE
    if info.st_size > MAX_SUBMISSION_BYTES:
        return "", SUBMISSION_TOO_LARGE
    with open(path, "rb") as handle:
        data = handle.read(MAX_SUBMISSION_BYTES + 1)
    if len(data) > MAX_SUBMISSION_BYTES:
        return "", SUBMISSION_TOO_LARGE
    try:
        return data.decode("utf-8"), SUBMISSION_OK
    except UnicodeDecodeError:
        return "", SUBMISSION_NOT_UTF8


def sandbox_environment(language: str, scratch: Path) -> tuple[dict[str, str], Path]:
    """Environment and working directory for the upstream executor."""
    env = {
        "PATH": DEFAULT_PATH,
        "HOME": str(scratch),
        "TMPDIR": str(scratch),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    cwd = scratch
    if language == "python":
        env["PATH"] = f"{TOOLS_ROOT}/python/bin:{DEFAULT_PATH}"
    elif language == "js":
        env["NODE_PATH"] = "/usr/local/lib/node_modules"
    elif language == "java":
        env["JAVA_HOME"] = f"{TOOLS_ROOT}/jdk"
        env["PATH"] = f"{TOOLS_ROOT}/jdk/bin:{DEFAULT_PATH}"
    elif language == "go":
        env["PATH"] = f"/usr/local/go/bin:{DEFAULT_PATH}"
        env["GO111MODULE"] = "off"
        env["GOPROXY"] = "off"
        env["GOPATH"] = f"{TOOLS_ROOT}/gopath"
        env["GOCACHE"] = f"{TOOLS_ROOT}/gocache"
        env["CGO_ENABLED"] = "0"
    elif language == "rust":
        env["RUSTUP_HOME"] = f"{TOOLS_ROOT}/rustup"
        env["CARGO_HOME"] = f"{TOOLS_ROOT}/cargo"
        env["CARGO_NET_OFFLINE"] = "true"
        env["PATH"] = f"{TOOLS_ROOT}/cargo/bin:{DEFAULT_PATH}"
        # The upstream executor builds in "<cwd>/rust". The image ships that
        # package there with upstream's Cargo.toml, a pinned Cargo.lock and
        # the dependencies already compiled, so only the solution is built.
        cwd = TOOLS_ROOT
    return env, cwd


def sandbox_pids(uid: int) -> list[int]:
    """Live (non-zombie) processes of the sandbox account in this container."""
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            status = Path("/proc", entry, "status").read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            continue  # the process ended while we were looking
        fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
        if fields.get("State", "").strip().startswith("Z"):
            continue
        if str(uid) in fields.get("Uid", "").split():
            pids.append(int(entry))
    return pids


def kill_sandbox_processes(uid: int, attempts: int = 200) -> bool:
    """SIGKILL everything the sandbox account still runs; True when nothing is left."""
    for _ in range(attempts):
        pids = sandbox_pids(uid)
        if not pids:
            return True
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(0.05)
    return not sandbox_pids(uid)


def run_in_sandbox(
    language: str, test_program: str, timeout: int, scratch: Path
) -> dict:
    """Run the upstream executor as the sandbox account and return what happened.

    The verdict is the child's exit status (0 passed, 1 failed). The text the
    child sends through the pipe is kept for the log only: the submitted code
    runs under the same account as the executor and could write to that pipe.
    """
    account = pwd.getpwnam(SANDBOX_USER)
    env, cwd = sandbox_environment(language, scratch)
    if language == "python":
        # Upstream executes the program with exec() in a fork of the harness
        # process, where numpy and the standard library are already imported
        # (the metric module imports numpy). Importing them here keeps that
        # property: inside the forked child they resolve from sys.modules.
        exec("\n".join(IMPORT_HELPER["python"]), {})  # noqa: S102

    sys.stdout.flush()
    sys.stderr.flush()
    read_end, write_end = os.pipe()
    child = os.fork()
    if child == 0:
        status = 3
        try:
            os.close(read_end)
            os.setsid()
            null = os.open(os.devnull, os.O_RDWR)
            for descriptor in (0, 1, 2):
                os.dup2(null, descriptor)
            os.closerange(3, write_end)
            os.closerange(write_end + 1, 4096)
            os.setgroups([])
            os.setgid(account.pw_gid)
            os.setuid(account.pw_uid)
            os.umask(0o022)
            os.environ.clear()
            os.environ.update(env)
            tempfile.tempdir = None
            os.chdir(cwd)
            outcome = execute.check_correctness(
                test_program,
                timeout,
                0,
                0,
                # code_eval_octopack names the JavaScript executor "javascript".
                language if language != "js" else "javascript",
                BASE_CARGO,
            )
            passed = outcome["passed"] is True
            message = {
                "passed": passed,
                "result": str(outcome["result"])[:RESULT_TEXT_LIMIT],
            }
            os.write(write_end, (json.dumps(message) + "\n").encode("utf-8"))
            status = 0 if passed else 1
        except BaseException as exc:  # noqa: BLE001  (nothing may leave this child)
            try:
                os.write(
                    write_end,
                    f"executor error: {type(exc).__name__}: {exc}\n".encode(
                        "utf-8", "replace"
                    ),
                )
            except OSError:
                pass
        finally:
            os._exit(status)

    os.close(write_end)
    os.set_blocking(read_end, False)
    deadline = time.monotonic() + timeout + 1 + GRACE_SEC
    received = bytearray()
    exit_status = None
    while exit_status is None and time.monotonic() < deadline:
        select.select([read_end], [], [], 0.05)
        _drain(read_end, received)
        pid, raw_status = os.waitpid(child, os.WNOHANG)
        if pid == child:
            exit_status = raw_status
    timed_out = exit_status is None
    clean = kill_sandbox_processes(account.pw_uid)
    if timed_out:
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass
        _, exit_status = os.waitpid(child, 0)
    _drain(read_end, received)
    os.close(read_end)

    if timed_out:
        ending = f"killed {GRACE_SEC} s after the time limit"
    elif os.WIFSIGNALED(exit_status):
        ending = f"signal {os.WTERMSIG(exit_status)}"
    else:
        ending = f"exit {os.WEXITSTATUS(exit_status)}"
    return {
        "exited_normally": not timed_out and os.WIFEXITED(exit_status),
        "exit_code": os.WEXITSTATUS(exit_status)
        if not timed_out and os.WIFEXITED(exit_status)
        else None,
        "ending": ending,
        "no_processes_left": clean,
        "text": bytes(received).decode("utf-8", "replace"),
    }


def _drain(descriptor: int, buffer: bytearray) -> None:
    while True:
        try:
            chunk = os.read(descriptor, 65536)
        except BlockingIOError:
            return
        if not chunk:
            return
        if len(buffer) < PIPE_READ_LIMIT:
            buffer.extend(chunk[: PIPE_READ_LIMIT - len(buffer)])


def executor_message(text: str, passed: bool) -> str:
    """The executor's result text: the last well-formed message that fits the verdict."""
    for line in reversed(text.splitlines()):
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(message, dict)
            and message.get("passed") is passed
            and isinstance(message.get("result"), str)
        ):
            return message["result"]
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--task", type=Path, default=Path("/tests/task.json"))
    parser.add_argument("--solution-dir", type=Path, default=Path("/app"))
    parser.add_argument("--log-dir", type=Path, default=Path("/logs/verifier"))
    args = parser.parse_args()

    if os.geteuid() != 0:
        raise RuntimeError(
            "the verifier must run as root to drop privileges for the submitted code"
        )

    task = json.loads(args.task.read_text(encoding="utf-8"))
    language = task["language"]
    timeout = LANGUAGE_TO_TIMEOUT[language]
    solution_path = args.solution_dir / task["solution_file"]

    generation, submission_status = read_submission(solution_path)
    if submission_status != SUBMISSION_OK:
        print(f"{solution_path}: {submission_status}; scoring the empty generation")

    test_program = (
        prepare_generation(language, generation, task) + "\n" + get_reference(task)
    )
    (args.log_dir / ("test_program" + solution_path.suffix)).write_text(
        test_program, encoding="utf-8"
    )

    account = pwd.getpwnam(SANDBOX_USER)
    scratch = Path(tempfile.mkdtemp(prefix="scoring-"))
    os.chown(scratch, account.pw_uid, account.pw_gid)
    scratch.chmod(0o700)

    started = time.monotonic()
    run = run_in_sandbox(language, test_program, timeout, scratch)
    scoring_seconds = round(time.monotonic() - started, 2)
    passed = run["exited_normally"] and run["exit_code"] == 0
    if run["exited_normally"] and run["exit_code"] in (0, 1):
        result = (
            executor_message(run["text"], passed)
            or f"(no result text; executor {run['ending']})"
        )
    elif run["exited_normally"]:
        # The upstream executor itself raised: this is not a verdict.
        raise RuntimeError(f"executor failed ({run['ending']}): {run['text'][-2000:]}")
    elif run["ending"].startswith("killed"):
        result = "timed out"
    else:
        result = f"failed: the scoring process ended abnormally ({run['ending']})"

    summary = {
        "task_id": task.get("task_id"),
        "upstream_task": task.get("upstream_task"),
        "upstream_task_id": task.get("upstream_task_id"),
        "language": language,
        "submission_status": submission_status,
        "passed": passed,
        "result": result[:RESULT_TEXT_LIMIT],
        "executor_ending": run["ending"],
        "no_processes_left": run["no_processes_left"],
        "scoring_seconds": scoring_seconds,
    }
    (args.log_dir / "result.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"task: {summary['task_id']} ({summary['upstream_task']}, {summary['upstream_task_id']})"
    )
    print(f"submission: {submission_status}")
    print(f"passed: {passed}")
    print("result:")
    print(summary["result"])
    return 0 if passed else 1


if __name__ == "__main__":
    try:
        status = main()
    except Exception as exc:  # noqa: BLE001  (a fault must not look like a verdict)
        print(f"verifier error: {type(exc).__name__}: {exc}")
        status = 2
    sys.exit(status)
