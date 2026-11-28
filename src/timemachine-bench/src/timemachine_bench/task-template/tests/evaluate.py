"""TimeMachine-bench grader for Harbor.

Runs in a fresh container of the task image. Steps:

1. Apply the agent's edits (collected by /opt/timemachine/collect.py into
   /logs/artifacts/timemachine/changes.tar) to /work. As with the upstream
   agent's tools, only edits to files that already exist in the repository are
   kept; edits to the record's test files and to the upstream setup/test
   scripts are discarded, and new or deleted files are ignored.
2. Run the upstream test script (`bash /work/test_<repo>.sh`) from /work with
   the upstream 600-second limit. Reward 1 if it exits with status 0, else 0:
   upstream's pass@1 is the exit status of the test container.
3. Compute upstream's prec@1 for the edits (line-level precision of the
   non-test .py edits against the gold patch) and write it, with the test log
   and grading details, to /logs/verifier/. It does not affect the reward.

Exit status 0 means a reward was written. Any other status means the
submission could not be graded.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tarfile
import tempfile
from collections import defaultdict
from itertools import groupby
from pathlib import Path, PurePosixPath

WORK = Path("/work")
TESTS = Path("/tests")
SUBMISSION = Path("/logs/artifacts/timemachine")
VERIFIER = Path("/logs/verifier")

# Upstream agents/src/main/solve_*.py: CONTAINER_TIMEOUT_SEC = 600.
TEST_TIMEOUT_SEC = 600


# ---------------------------------------------------------------------------
# prec@1, copied from upstream agents/src/evaluation/calc_metrics.py
# (tohoku-nlp/timemachine-bench, Apache-2.0).
# ---------------------------------------------------------------------------
# fmt: off
regex_file_separator = re.compile(r"(?=^--- )", re.MULTILINE)
regex_file_header = re.compile(r"^--- ([^\t\n]+)$")
regex_diff_header_start = re.compile(r"(?=^@@ )", re.MULTILINE)
regex_diff_header = re.compile(r"^@@ \-(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")


def extract_edits_from_diff(patch_path: Path):
    with open(patch_path) as f:
        diff_text = f.read()

    edit_by_file_sets = defaultdict(set)

    file_diffs = regex_file_separator.split(diff_text)

    for file_diff in file_diffs:
        if not file_diff.strip():
            continue

        file_name = regex_file_header.match(file_diff.splitlines()[0]).group(1)

        diff_blocks = regex_diff_header_start.split(file_diff)

        for diff in diff_blocks:
            # skip file header
            if not diff.startswith("@@"):
                continue

            header, *content = diff.splitlines(keepends=True)
            header_match = regex_diff_header.match(header)

            if not header_match:
                continue

            current_line = int(header_match.group(1))

            for flg, grp in groupby(content, key=lambda x: x.startswith(("+", "-"))):
                grp_lines = list(grp)

                if not flg:
                    # skip unchanged lines
                    current_line += len(grp_lines)
                    continue

                is_deleted_or_modified = any(line.startswith("-") for line in grp_lines)

                if is_deleted_or_modified:
                    for line in grp_lines:
                        if line.startswith("-"):
                            edit_by_file_sets[file_name].add(current_line)
                            current_line += 1
                else:
                    # added, record line number where the code is inserted once
                    edit_by_file_sets[file_name].add(current_line)
                    continue

    return edit_by_file_sets


def calc_prec(gold_edits, model_edits):
    # expand the dict to make a set of "{file_name}_{line_number}" strings for comparison
    model_edits = {f"{file_name}_{line_number}" for file_name, lines in model_edits.items() for line_number in lines}
    gold_edits = {f"{file_name}_{line_number}" for file_name, lines in gold_edits.items() for line_number in lines}

    total_model_edits = len(model_edits)

    tp = len(model_edits.intersection(gold_edits))

    if total_model_edits == 0:
        # the model cannot make tests pass without edits
        prec = 0
    else:
        prec = tp / total_model_edits

    return prec
# fmt: on


# ---------------------------------------------------------------------------
# Applying the submission
# ---------------------------------------------------------------------------
def safe_relpath(name: str) -> str | None:
    """Return a normalized path relative to /work, or None if it escapes it."""
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        return None
    rel = path.as_posix()
    if rel.startswith("./"):
        rel = rel[2:]
    return rel or None


def is_existing_repo_file(rel: str) -> bool:
    dest = WORK / rel
    if dest.is_symlink() or not dest.is_file():
        return False
    try:
        dest.resolve().relative_to(WORK.resolve())
    except ValueError:
        return False
    return True


def apply_submission(protected: set[str]) -> tuple[list[str], list[dict], dict[str, bytes]]:
    applied: list[str] = []
    discarded: list[dict] = []
    originals: dict[str, bytes] = {}
    with tarfile.open(SUBMISSION / "changes.tar") as tar:
        for member in tar.getmembers():
            rel = safe_relpath(member.name)
            if rel is None or not member.isfile():
                discarded.append({"path": member.name, "reason": "not a regular file inside /work"})
                continue
            if rel in protected:
                discarded.append({"path": rel, "reason": "test file or upstream setup/test script"})
                continue
            if not is_existing_repo_file(rel):
                discarded.append({"path": rel, "reason": "not an existing file of the repository"})
                continue
            data = tar.extractfile(member).read()
            dest = WORK / rel
            originals[rel] = dest.read_bytes()
            dest.write_bytes(data)
            applied.append(rel)
    return applied, discarded, originals


def model_patch(originals: dict[str, bytes], test_files: set[str]) -> str:
    """Unified diff of the applied non-test .py files, in the format of upstream
    agents/src/evaluation/get_patch.sh (`diff -Naur` with bare path headers)."""
    chunks: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        for rel in sorted(originals):
            if not rel.endswith(".py") or rel in test_files:
                continue
            before = Path(tmp) / "before.py"
            before.write_bytes(originals[rel])
            proc = subprocess.run(
                ["diff", "-Naur", "--label", rel, "--label", rel, str(before), str(WORK / rel)],
                stdout=subprocess.PIPE,
                check=False,
            )
            chunks.append(proc.stdout.decode("utf-8", errors="replace"))
    return "".join(chunks)


# ---------------------------------------------------------------------------
# Running the tests
# ---------------------------------------------------------------------------
def run_tests(test_script: str, log_path: Path) -> tuple[int | None, bool]:
    with log_path.open("wb") as log:
        proc = subprocess.Popen(
            ["bash", f"/work/{test_script}"],
            cwd=WORK,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            return proc.wait(timeout=TEST_TIMEOUT_SEC), False
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            return None, True


def main() -> int:
    VERIFIER.mkdir(parents=True, exist_ok=True)
    info = json.loads((TESTS / "task_info.json").read_text())
    test_files = set(info["test_files"])
    protected = test_files | {info["setup_script"], info["test_script"]}

    if not (SUBMISSION / "changes.tar").is_file():
        print(
            f"No submission at {SUBMISSION}/changes.tar: the collect hook did not run "
            "or failed, so the trial cannot be graded.",
            file=sys.stderr,
        )
        return 2

    summary_path = SUBMISSION / "summary.json"
    summary = json.loads(summary_path.read_text()) if summary_path.is_file() else {}

    applied, discarded, originals = apply_submission(protected)
    patch_text = model_patch(originals, test_files)
    patch_path = VERIFIER / "model.patch"
    patch_path.write_text(patch_text)
    prec = calc_prec(
        extract_edits_from_diff(TESTS / "gold.patch"),
        extract_edits_from_diff(patch_path),
    )

    returncode, timed_out = run_tests(info["test_script"], VERIFIER / "test_output.log")
    passed = returncode == 0
    (VERIFIER / "reward.txt").write_text("1" if passed else "0")

    details = {
        "repo_name": info["repo_name"],
        "pass": passed,
        "prec": prec,
        "test_returncode": returncode,
        "test_timed_out": timed_out,
        "applied_files": applied,
        "discarded_files": discarded,
        "deleted_files_ignored": summary.get("deleted", []),
        "new_files_ignored": summary.get("new", []),
    }
    (VERIFIER / "metrics.json").write_text(json.dumps(details, indent=2))
    print(
        json.dumps({k: details[k] for k in ("pass", "prec", "test_returncode", "test_timed_out")})
    )
    print(f"applied {len(applied)} file(s): {applied}")
    if discarded:
        print(f"discarded {len(discarded)} file(s): {discarded}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
