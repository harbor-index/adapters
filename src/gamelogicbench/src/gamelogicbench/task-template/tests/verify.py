"""Standalone Harbor entry point for the frozen GameLogicBench judge."""

from __future__ import annotations

import errno
import json
import os
import resource
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


WORKSPACE = Path("/workspace/game")
PRIVATE = Path("/tests/private")
LOGS = Path("/logs/verifier")
RUNTIME_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
FIRST_WORKER_UID = 42000
MAX_RESULT_BYTES = 256 * 1024
UNUSABLE = {"infra_error", "unknown_scenario", "no_press_axis", "unknown_press_axis"}
ABNORMAL_ENGINE_SIGNALS = (signal.SIGABRT, signal.SIGBUS, signal.SIGFPE, signal.SIGILL, signal.SIGSEGV)


class InvalidSubmission(Exception):
    """The workspace cannot be used as an upstream game project."""


class VerifierError(Exception):
    """The verifier failed to reach a reliable verdict."""


@dataclass(frozen=True)
class Cell:
    scenario: str
    seed: int
    press: str
    tier: str


@dataclass(frozen=True)
class Verdict:
    outcome: str
    passed: bool | None


def deliverable_path(value: str) -> Path:
    if not value.startswith("res://"):
        raise VerifierError("Deliverable must be a res:// path.")
    relative = value.removeprefix("res://").rstrip("/")
    if not relative or any(part in {"", ".", ".."} for part in relative.split("/")):
        raise VerifierError("Invalid deliverable path in evaluation manifest.")
    return Path(relative)


def validate_workspace(workspace: Path, deliverables: list[Path]) -> None:
    if workspace.is_symlink() or not workspace.is_dir():
        raise InvalidSubmission("The submitted game directory is missing.")
    for parent, directories, files in os.walk(workspace):
        for name in (*directories, *files):
            entry = Path(parent, name)
            mode = entry.lstat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise InvalidSubmission("The submitted game has a non-regular entry.")
    if not (workspace / "project.godot").is_file():
        raise InvalidSubmission("The submitted game has no project.godot.")
    if any(not (workspace / relative).exists() for relative in deliverables):
        raise InvalidSubmission("A required game deliverable is missing.")


def copy_deliverable(source: Path, target: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def overlay(source: Path, destination: Path) -> None:
    for parent, directories, files in os.walk(source):
        relative = Path(parent).relative_to(source)
        for name, expected_dir in [(name, True) for name in directories] + [
            (name, False) for name in files
        ]:
            target = destination / relative / name
            if target.exists() and target.is_dir() != expected_dir:
                raise InvalidSubmission("The submitted game conflicts with frozen files.")
    shutil.copytree(source, destination, dirs_exist_ok=True)


def prepare_project(
    workspace: Path,
    frozen_judge: Path,
    cell_root: Path,
    deliverables: list[Path],
    invert_overlay: bool,
) -> Path:
    """Use the submitted game as the base and preserve specified Repo deliverables."""
    project = cell_root / "project"
    shutil.copytree(workspace, project)
    if invert_overlay:
        snapshot = cell_root / "deliverables"
        for relative in deliverables:
            copy_deliverable(project / relative, snapshot / relative)
    overlay(frozen_judge, project)
    if invert_overlay:
        overlay(snapshot, project)
        shutil.rmtree(snapshot)
    return project


def prepare_worker_tree(root: Path, uid: int) -> None:
    os.chown(root, uid, uid)
    root.chmod(0o700)
    for parent, directories, files in os.walk(root):
        for name in directories:
            directory = Path(parent, name)
            os.chown(directory, uid, uid)
            directory.chmod(0o700)
        for name in files:
            file = Path(parent, name)
            executable = bool(file.stat().st_mode & 0o111)
            os.chown(file, uid, uid)
            file.chmod(0o700 if executable else 0o600)


def godot_commands(
    project: Path, output: Path, cell: Cell, manifest: dict[str, Any]
) -> list[list[str]]:
    scene = ["res://judge.tscn", "--", "--scenario", cell.scenario, "--seed", str(cell.seed)]
    if cell.press:
        scene.extend(["--press", cell.press])
    scene.extend(["--controller", manifest["deliverable_paths"][0], "--out", str(output)])
    headless = ["godot", "--headless", "--path", str(project)]
    if manifest["mode"] == "headless":
        return [headless + scene]
    window = manifest["window"]
    return [
        headless + ["--import"],
        [
            "xvfb-run", "-a", "-s", f"-screen 0 {window['width']}x{window['height']}x24",
            "godot", "--path", str(project), "--rendering-driver", "opengl3", *scene,
        ],
    ]


def worker_processes(uid: int) -> list[int]:
    """Find this verifier's worker processes for cleanup, including detached children."""
    found = []
    for process in Path("/proc").iterdir():
        if not process.name.isdecimal():
            continue
        try:
            for line in (process / "status").read_text().splitlines():
                if line.startswith("Uid:"):
                    if int(line.split()[1]) == uid:
                        found.append(int(process.name))
                    break
        except (FileNotFoundError, ProcessLookupError):
            continue
    return found


def cleanup_worker(uid: int) -> None:
    for pid in worker_processes(uid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue


def oom_kills() -> int:
    try:
        entries = Path("/sys/fs/cgroup/memory.events").read_text().splitlines()
    except OSError:
        return 0
    return next((int(line.split()[1]) for line in entries if line.startswith("oom_kill ")), 0)


def run_worker(
    command: list[str],
    uid: int,
    cell_root: Path,
    env: dict[str, str],
    timeout: float,
    kill_after: float,
    pids_limit: int,
) -> int:
    if timeout <= 0:
        raise subprocess.TimeoutExpired(command, timeout)

    def limit_processes() -> None:
        resource.setrlimit(resource.RLIMIT_NPROC, (pids_limit, pids_limit))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    isolated_command = ["setpriv", "--no-new-privs", *command]
    with subprocess.Popen(
        isolated_command, cwd=cell_root, env=env, stdin=subprocess.DEVNULL,
        stdout=sys.stdout, stderr=sys.stderr,
        start_new_session=True, preexec_fn=limit_processes,
        user=uid, group=uid, extra_groups=(),
    ) as process:
        try:
            return process.wait(timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(kill_after)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            raise


def read_verdict(file: Path, uid: int, cell: Cell, returncode: int, out_of_memory: bool) -> Verdict:
    try:
        fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if out_of_memory:
            return Verdict("killed_oom", False)
        if returncode == 1 or any(
            returncode in {-code, 128 + code} for code in ABNORMAL_ENGINE_SIGNALS
        ):
            return Verdict("crashed", False)
        raise VerifierError(f"Judge exited without a usable result (exit {returncode}).")
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise InvalidSubmission("The judge output must be a regular file.") from error
        raise
    with os.fdopen(fd, "rb") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != uid or metadata.st_nlink != 1:
            raise InvalidSubmission("The judge output has an invalid file type or owner.")
        contents = handle.read(MAX_RESULT_BYTES + 1)
    if len(contents) > MAX_RESULT_BYTES:
        raise InvalidSubmission("The judge output is too large.")
    raw = json.loads(contents)
    if not isinstance(raw, dict):
        raise VerifierError("Expected a JSON object from the frozen judge.")
    if raw.get("scenario", cell.scenario) != cell.scenario or raw.get("seed", cell.seed) != cell.seed:
        raise VerifierError("The frozen judge returned the wrong scenario or seed.")
    outcome = raw.get("outcome", "pass")
    if not isinstance(outcome, str):
        raise VerifierError("Unexpected outcome from the frozen judge.")
    if outcome in UNUSABLE:
        return Verdict(outcome, None)
    passed = raw.get("pass", False)
    if not isinstance(passed, bool) or returncode != (0 if passed else 1):
        raise VerifierError("The frozen judge returned inconsistent verdict signals.")
    return Verdict("controller_invalid" if outcome == "build_error" else outcome, passed)


def evaluate_cell(
    manifest: dict[str, Any], cell: Cell, uid: int, temporary_root: Path, deliverables: list[Path]
) -> Verdict:
    if worker_processes(uid):
        raise VerifierError("Worker UID is already in use.")
    with tempfile.TemporaryDirectory(prefix="cell-", dir=temporary_root) as temporary:
        cell_root = Path(temporary)
        project = prepare_project(
            WORKSPACE, PRIVATE / "judge", cell_root, deliverables, manifest["invert_overlay"]
        )
        home = cell_root / "home"
        scratch = cell_root / "tmp"
        home.mkdir()
        scratch.mkdir()
        output = cell_root / "result.json"
        prepare_worker_tree(cell_root, uid)
        env = {
            "PATH": RUNTIME_PATH,
            "HOME": str(home),
            "TMPDIR": str(scratch),
            "XDG_DATA_HOME": str(home / ".local/share"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "LANG": "C.UTF-8",
            "GODOT_SILENCE_ROOT_WARNING": "1",
        }
        if manifest["mode"] == "rendered":
            env.update(LIBGL_ALWAYS_SOFTWARE="1", LP_NUM_THREADS=str(manifest["render_threads"]))
        commands = godot_commands(project, output, cell, manifest)
        timeouts = manifest["timeout"]
        start = time.monotonic()
        initial_oom_kills = oom_kills()
        try:
            for command in commands:
                returncode = run_worker(
                    command, uid, cell_root, env,
                    timeouts["container"] - (time.monotonic() - start),
                    timeouts["kill_after"], manifest["resources"]["pids_limit"],
                )
        except subprocess.TimeoutExpired:
            return Verdict("killed_timeout", False)
        finally:
            cleanup_worker(uid)
        return read_verdict(output, uid, cell, returncode, oom_kills() > initial_oom_kills)


def evaluate() -> int:
    if os.geteuid() != 0:
        raise VerifierError("The verifier supervisor requires root to isolate judge workers.")
    manifest = json.loads((PRIVATE / "evaluation.json").read_text())
    if manifest["mode"] not in {"headless", "rendered"}:
        raise VerifierError("Unsupported judge mode.")
    cells = [Cell(**cell) for cell in manifest["cells"]]
    if not cells or len({(cell.scenario, cell.seed) for cell in cells}) != len(cells):
        raise VerifierError("The evaluation manifest has an empty or duplicate cell matrix.")
    deliverables = [deliverable_path(value) for value in manifest["deliverable_paths"]]
    if not deliverables or not (PRIVATE / "judge" / "judge.tscn").is_file():
        raise VerifierError("The verifier is missing a frozen judge or deliverable declaration.")
    if manifest["mode"] == "rendered" and not shutil.which("xvfb-run", path=RUNTIME_PATH):
        raise VerifierError("Rendered judging requires Xvfb.")
    probe = subprocess.run(
        ["setpriv", "--no-new-privs", "godot", "--headless", "--version"],
        env={"PATH": RUNTIME_PATH}, capture_output=True, text=True, timeout=15, check=True,
        user=FIRST_WORKER_UID, group=FIRST_WORKER_UID, extra_groups=(), cwd="/",
    )
    if probe.stdout.strip() != "4.4.stable.official.4c311cbee":
        raise VerifierError("The frozen judge requires the pinned Godot 4.4 build.")
    validate_workspace(WORKSPACE, deliverables)
    counts = {"public": {"passed": 0, "total": 0}, "hidden": {"passed": 0, "total": 0}}
    with (LOGS / "cells.jsonl").open("w") as results, tempfile.TemporaryDirectory(
        prefix="gamelogicbench-"
    ) as temporary:
        temporary_root = Path(temporary)
        temporary_root.chmod(0o711)
        for index, cell in enumerate(cells):
            verdict = evaluate_cell(manifest, cell, FIRST_WORKER_UID + index,
                                    temporary_root, deliverables)
            results.write(
                json.dumps({"task": manifest["task_id"], **asdict(cell), **asdict(verdict)}) + "\n"
            )
            results.flush()
            if verdict.passed is None:
                raise VerifierError(f"Frozen judge cell {index + 1} is unusable: {verdict.outcome}.")
            counts[cell.tier]["passed"] += int(verdict.passed)
            counts[cell.tier]["total"] += 1
    print(json.dumps({"task": manifest["task_id"], "counts": counts}))
    return int(sum(bucket["passed"] for bucket in counts.values()) == len(cells))


def main() -> int:
    LOGS.mkdir(mode=0o700, parents=True, exist_ok=True)
    LOGS.chmod(0o700)
    reward_path = LOGS / "reward.txt"
    reward_path.unlink(missing_ok=True)
    try:
        reward = evaluate()
    except InvalidSubmission as error:
        print(f"Invalid submission: {error}", file=sys.stderr)
        reward = 0
    except (VerifierError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"Verifier error: {error}", file=sys.stderr)
        return 2
    reward_path.write_text(f"{reward}\n")
    return 0 if reward else 1


if __name__ == "__main__":
    sys.exit(main())
