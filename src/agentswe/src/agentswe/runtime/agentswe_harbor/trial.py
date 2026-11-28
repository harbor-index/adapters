from __future__ import annotations

import contextlib
import json
import os
import pwd
import shutil
import signal
import subprocess
import tomllib
import traceback
from datetime import datetime, timezone
from pathlib import Path

from . import settings
from .brokers import BASE_ENV

AGENT_DIR, VERIFIER_DIR, ARTIFACTS_DIR = Path("/logs/agent"), Path("/logs/verifier"), Path("/logs/artifacts")
WORKDIR = Path("/workspace")
# Mount targets that upstream's per-case compose files use; any other target is rejected.
MOUNT_ROOTS = ("/tests", "/solution", "/submission", "/active-case", "/candidate-output", "/evaluator", "/harness",
               "/trusted-evidence", "/run/secrets", "/opt/agentswe", "/opt/agentswe-trusted-browser", "/tools")
# Upstream binds these tools from the host; the evaluator images install the same tools.
HOST_TOOL_ROOTS = ("/usr/", "/etc/", "/lib/")
# Large read-only trees (environments, trusted bundles) are linked instead of copied.
LINKED_SOURCES = (settings.CANDIDATE_ENV_PREFIX, settings.TRUSTED_ENV_PREFIX, Path("/opt/agentswe-deps"))


class AgentTimeoutError(Exception):
    pass


class VerifierTimeoutError(Exception):
    pass


class RewardFileNotFoundError(FileNotFoundError):
    pass


def remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.is_symlink() or path.exists():
        path.unlink()


def clear(path: Path) -> None:
    """Empty a directory in place: the outer Harbor verifier mounts /logs/verifier."""
    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        remove(child)
    path.chmod(0o777)


def linked(source: Path) -> bool:
    return any(source == root or root in source.parents for root in LINKED_SOURCES)


@contextlib.contextmanager
def step(task_dir: Path, scripts: str, compose: Path):
    """One job step as its upstream compose file declares it: the step's scripts at /<scripts> and every bind
    mount recreated (copied, or linked for large read-only trees), all removed when the step ends."""
    services = json.loads(compose.read_text(encoding="utf-8"))["services"]
    if set(services) != {"main"}:
        raise RuntimeError(f"{compose}: services other than main are not supported: {sorted(services)}")
    main = services["main"]
    created = [Path("/" + scripts)]
    try:
        # The verifier image holds Harbor's own /tests/test.sh, which has already exec'd into this process.
        remove(created[0])
        shutil.copytree(task_dir / scripts, created[0], symlinks=True)
        # Parents before children, as Docker orders nested bind mounts.
        for volume in sorted(main["volumes"], key=lambda volume: len(Path(volume["target"]).parts)):
            source, target = Path(volume["source"]), Path(volume["target"])
            if source == target:
                continue
            if str(target).startswith(HOST_TOOL_ROOTS):
                if not target.exists():
                    raise RuntimeError(f"host tool missing from the image: {target}")
                continue
            if not any(str(target) == root or str(target).startswith(root + "/") for root in MOUNT_ROOTS):
                raise RuntimeError(f"unsupported mount target: {target}")
            if not source.exists():
                raise RuntimeError(f"mount source missing: {source}")
            if linked(source) and target.is_dir() and not target.is_symlink():
                raise RuntimeError(f"refusing to replace directory {target} with a link")
            remove(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            if linked(source):
                target.symlink_to(source, target_is_directory=True)
            elif source.is_dir():
                shutil.copytree(source, target, symlinks=True)
            else:
                shutil.copy2(source, target)
            created.append(target)
        targets = {Path(volume["target"]) for volume in main["volumes"]}
        yield {key: str(value) for key, value in main.get("environment", {}).items()}, targets
    finally:
        for path in reversed(created):
            remove(path)


def kill_user_processes(uid: int) -> None:
    """Upstream discards a Candidate's container after its step; here its leftover processes go."""
    for entry in Path("/proc").iterdir():
        with contextlib.suppress(FileNotFoundError, ProcessLookupError):
            if entry.name.isdigit() and entry.stat().st_uid == uid:
                os.kill(int(entry.name), signal.SIGKILL)


def run_script(script: str, environment: dict[str, str], timeout: float, log: Path, user: str) -> int | None:
    """Run a step script as `user`; None when it times out. Nothing the step started outlives it."""
    account = pwd.getpwnam(user)
    with log.open("wb") as output:
        process = subprocess.Popen(
            ["bash", script], env={**BASE_ENV, "HOME": account.pw_dir, **environment}, cwd=WORKDIR,
            stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
            user=account.pw_uid, group=account.pw_gid, extra_groups=[],
        )
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            if account.pw_uid != 0:
                kill_user_processes(account.pw_uid)


def rewards(verifier_dir: Path) -> dict:
    if (verifier_dir / "reward.json").is_file():
        return json.loads((verifier_dir / "reward.json").read_text(encoding="utf-8"))
    if (verifier_dir / "reward.txt").is_file():
        return {"reward": float((verifier_dir / "reward.txt").read_text(encoding="utf-8"))}
    raise RewardFileNotFoundError(f"no reward file in {verifier_dir}")


def run_trial(task_dir: Path, trial_dir: Path) -> None:
    """One staged upstream per-case task as Harbor runs it with the oracle agent (solution/solve.sh) and a
    separate verifier (tests/test.sh), recorded in Harbor's trial layout for upstream's collectors."""
    config = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
    result = {
        "task_name": task_dir.name,
        "trial_name": trial_dir.name,
        "config": {"task": {"path": str(task_dir)}},
        "started_at": datetime.now(timezone.utc).isoformat(),
        "exception_info": None,
        "verifier_result": None,
    }
    try:
        for directory in (AGENT_DIR, VERIFIER_DIR, ARTIFACTS_DIR):
            clear(directory)
        with step(task_dir, "solution", task_dir / "environment" / "docker-compose.yaml") as (environment, targets):
            # A job that runs the delivered submission runs it unprivileged: upstream isolates it in a container of
            # its own, here it shares the evaluator's.
            user = settings.CANDIDATE_USER if Path("/submission") in targets else config["agent"]["user"]
            code = run_script("/solution/solve.sh", environment, config["agent"]["timeout_sec"],
                              AGENT_DIR / "oracle.txt", user)
            if code is None:
                raise AgentTimeoutError(f"solve.sh exceeded {config['agent']['timeout_sec']} s")
            if code:
                (AGENT_DIR / "exit-code.txt").write_text(str(code), encoding="utf-8")
        with step(task_dir, "tests", task_dir / "tests" / "docker-compose.yaml") as (environment, _):
            code = run_script("/tests/test.sh", environment, config["verifier"]["timeout_sec"],
                              VERIFIER_DIR / "test-stdout.txt", config["verifier"]["user"])
            if code is None:
                raise VerifierTimeoutError(f"test.sh exceeded {config['verifier']['timeout_sec']} s")
        result["verifier_result"] = {"rewards": rewards(VERIFIER_DIR)}
    except Exception as exc:
        # Harbor records a trial's exception; upstream's assert_runtime_health then withholds the phase.
        result["exception_info"] = {"exception_type": type(exc).__name__, "exception_message": str(exc),
                                    "exception_traceback": traceback.format_exc()}
    finally:
        for source, destination in ((AGENT_DIR, trial_dir / "agent"), (VERIFIER_DIR, trial_dir / "verifier"),
                                    (ARTIFACTS_DIR, trial_dir / "artifacts" / "logs" / "artifacts")):
            shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        (trial_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
