"""Entry point of the simulator container.

Runs the unchanged native RoboDojo evaluation for one canonical task with the
``HarborDirect`` policy: first the native policy server, which hosts the
broker, then the native simulator client once per variant. Native code keeps
ownership of layouts, invalid-layout replacement, termination, scoring and the
``_result.json`` files.

The supervisor writes ``supervisor.json`` as its final verdict and then stays
alive, so that Harbor can collect the results directory from this container.
It never computes or edits a score.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from typing import Any
import urllib.error
import urllib.request
import uuid

from ..catalog import XPOLICYLAB_REVISION
from ..runtime import EvaluationError, TaskSpec, load_spec, read_json, reserve_results, write_json

POLICY_NAME = "HarborDirect"
CHECKPOINT_LABEL = "direct"
ACTION_LABEL = "joint"
POLICY_PORT = 19000
BRIDGE_PORT = 19100
SERVER_START_TIMEOUT = 600
# One native variant: simulator time plus every model decision of 25 or 50 episodes.
VARIANT_TIMEOUT = 14 * 24 * 3600


def native_result_path(root: Path, variant: str, seed: int, run_id: str) -> Path:
    return (
        root / "eval_result" / "RoboDojo" / variant / POLICY_NAME / "arx_x5"
        / f"{seed}_ckpt_name={CHECKPOINT_LABEL},action_type={ACTION_LABEL}" / run_id / "_result.json"
    )


def find_native_result(root: Path, variant: str, seed: int, run_id: str, started_ns: int) -> Path:
    """Locate only this invocation's result, rejecting stale or ambiguous output."""
    scope = root / "eval_result" / "RoboDojo" / variant / POLICY_NAME
    candidates = [path for path in scope.rglob("_result.json") if run_id in path.parts]
    expected = native_result_path(root, variant, seed, run_id)
    if len(candidates) != 1 or candidates[0] != expected:
        raise EvaluationError("Expected exactly one native result for this variant, seed and run ID")
    if expected.is_symlink() or expected.resolve() != expected.absolute():
        raise EvaluationError("Native result path must not contain symlinks")
    if expected.stat().st_mtime_ns < started_ns:
        raise EvaluationError("Native result is stale")
    return expected


def verify_image(root: Path, spec: TaskSpec) -> None:
    for filename, expected in (
        (".harbor-upstream-revision", spec.upstream_revision),
        (".harbor-xpolicylab-revision", XPOLICYLAB_REVISION),
    ):
        try:
            revision = (root / filename).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise EvaluationError(f"Cannot verify image revision marker {filename}: {exc}") from exc
        if revision != expected:
            raise EvaluationError(f"Image marker {filename} does not match the pinned revision")
    if not (root / "scripts" / "robodojo.sh").is_file():
        raise EvaluationError("Simulator entry point scripts/robodojo.sh is missing")


def install_policy(root: Path, source: Path, cleanup: ExitStack | None) -> Path:
    """Install the policy next to the native ones. It adds files and changes none."""
    target = root / "XPolicyLab" / "policy" / POLICY_NAME
    if target.exists() or target.is_symlink():
        raise EvaluationError(f"Policy directory already exists: {target}")
    if not target.parent.is_dir():
        raise EvaluationError("XPolicyLab policy directory is missing from the image")
    target.mkdir()
    if cleanup is not None:
        cleanup.callback(shutil.rmtree, target)
    shutil.copytree(source, target, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__"))
    return target


def wait_for_bridge(server: subprocess.Popen, port: int, timeout: float, results_dir: Path) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise EvaluationError(f"Policy server exited early with code {server.returncode}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=5) as response:
                status = json.load(response)
                marker = read_json(results_dir / "broker" / "attempt.json")
                if status["attempt_id"] == marker["attempt_id"]:
                    return marker["attempt_id"]
        except (OSError, ValueError, KeyError, EvaluationError):
            pass
        time.sleep(1)
    raise EvaluationError("Policy server did not open the bridge in time")


def _run_client(command: list[str], *, timeout: float, **kwargs: Any) -> None:
    with subprocess.Popen(command, start_new_session=True, **kwargs) as client:
        try:
            code = client.wait(timeout=timeout)
        except BaseException:
            _terminate(client)
            raise
        if code:
            raise subprocess.CalledProcessError(code, command)


def supervise(
    spec_path: Path, root: Path, results_dir: Path, policy_source: Path,
    *, popen: Callable[..., Any] = subprocess.Popen, run: Callable[..., Any] = _run_client,
    wait: Callable[..., str] = wait_for_bridge, standalone: bool = False,
) -> dict[str, Any]:
    """Own one results directory, publish its verdict, then release standalone resources."""
    root, results_dir = root.resolve(), results_dir.resolve()
    reserve_results(results_dir)
    with ExitStack() as cleanup:
        try:
            verdict = _run_native(
                spec_path, root, results_dir, policy_source, cleanup if standalone else None,
                popen, run, wait,
            )
        except (EvaluationError, OSError) as exc:
            write_json(results_dir / "supervisor.json", {"status": "failed", "error": str(exc)})
            raise
        write_json(results_dir / "supervisor.json", verdict)
        return verdict


def _run_native(
    spec_path: Path, root: Path, results_dir: Path, policy_source: Path, cleanup: ExitStack | None,
    popen: Callable[..., Any], run: Callable[..., Any], wait: Callable[..., str],
) -> dict[str, Any]:
    logs = results_dir / "logs"
    spec = load_spec(spec_path)
    if spec.mock:
        raise EvaluationError("The mock task runs in the mock service, not in the native simulator")
    verify_image(root, spec)
    policy_dir = install_policy(root, policy_source, cleanup)

    environment = os.environ.copy()
    environment.update(
        HARBOR_ROBODOJO_RESULTS=str(results_dir),
        HARBOR_ROBODOJO_BRIDGE_PORT=str(BRIDGE_PORT),
        EVAL_NUM="native",
    )
    if cleanup is not None:
        environment["HARBOR_ROBODOJO_BRIDGE_HOST"] = "127.0.0.1"
    server_command = [
        "bash", str(root / "scripts" / "robodojo.sh"), "server",
        "--policy-dir", str(policy_dir), "--task", spec.task_id, "--ckpt", CHECKPOINT_LABEL,
        "--policy-env", "RoboDojo", "--policy-port", str(POLICY_PORT), "--bind-host", "127.0.0.1",
        "--action-type", ACTION_LABEL, "--seed", str(spec.seed),
    ]
    variants = []
    with (logs / "policy_server.log").open("w", encoding="utf-8") as server_log:
        server = popen(
            server_command, cwd=root, env=environment, stdout=server_log,
            stderr=subprocess.STDOUT, start_new_session=True,
        )
        if cleanup is not None:
            cleanup.callback(_stop_standalone_server, server, results_dir)
        try:
            attempt_id = wait(server, BRIDGE_PORT, SERVER_START_TIMEOUT, results_dir)
            for variant in spec.variants:
                run_id = f"harbor_{uuid.uuid4().hex}"
                if native_result_path(root, variant.name, spec.seed, run_id).parent.exists():
                    raise EvaluationError("Invocation run directory already exists")
                command = [
                    "bash", str(root / "scripts" / "robodojo.sh"), "client",
                    "--task", variant.name, "--policy-name", POLICY_NAME,
                    "--policy-host", "127.0.0.1", "--policy-port", str(POLICY_PORT),
                    "--seed", str(spec.seed), "--eval-num", "native",
                    "--ckpt", CHECKPOINT_LABEL, "--action-type", ACTION_LABEL,
                ]
                started_ns = time.time_ns()
                write_json(results_dir / f"{variant.name}.run.json", {
                    "command": command, "run_id": run_id, "started_ns": started_ns,
                })
                try:
                    with (logs / f"{variant.name}.log").open("w", encoding="utf-8") as log:
                        run(
                            command, cwd=root, env={**environment, "ROBODOJO_RUN_ID": run_id},
                            stdout=log, stderr=subprocess.STDOUT, timeout=VARIANT_TIMEOUT,
                        )
                except (OSError, subprocess.SubprocessError) as exc:
                    raise EvaluationError(f"Native simulator client failed for {variant.name}: {exc}") from exc
                if server.poll() is not None:
                    raise EvaluationError("Policy server exited during the evaluation")
                artifact = find_native_result(root, variant.name, spec.seed, run_id, started_ns)
                # Check that the file parses before publishing an exact copy.
                read_json(artifact)
                destination = results_dir / "native" / variant.name / "_result.json"
                destination.parent.mkdir(parents=True)
                shutil.copyfile(artifact, destination)
                variants.append({"name": variant.name, "episodes": variant.episodes, "run_id": run_id})
        except EvaluationError:
            # Keep the broker reachable so that the agent can read the failure.
            raise
        except BaseException:
            if cleanup is None:
                _terminate(server)
            raise
    # The policy server keeps running so that the agent can read the final status.
    return {
        "status": "completed", "attempt_id": attempt_id, "task_id": spec.task_id, "dimension": spec.dimension,
        "seed": spec.seed, "upstream_revision": spec.upstream_revision,
        "xpolicylab_revision": XPOLICYLAB_REVISION, "policy_name": POLICY_NAME,
        "variants": variants,
    }


def _stop_standalone_server(server: subprocess.Popen, results_dir: Path) -> None:
    # Give the waiting player time to receive the terminal HTTP response.
    broker = results_dir / "broker"
    try:
        if (broker / "attempt.json").exists() and (results_dir / "supervisor.json").exists():
            deadline = time.monotonic() + 30
            while server.poll() is None and time.monotonic() < deadline:
                if (broker / "verdict-delivered").exists() or (broker / "abort.json").exists():
                    break
                time.sleep(0.1)
    finally:
        _terminate(server)


def _terminate(process: subprocess.Popen) -> None:
    # Native launchers spawn children. Stop the whole session's process group.
    if process.pid <= 1:
        raise ValueError("Expected a child process group")
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        process.poll()  # Reap the group leader if it has already exited.
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    home = Path(os.environ.get("HARBOR_ROBODOJO_HOME", "/opt/harbor-robodojo"))
    parser.add_argument("--spec", type=Path, default=home / "task_spec.json")
    parser.add_argument("--root", type=Path, default=Path("/workspace/RoboDojo"))
    parser.add_argument("--results-dir", type=Path, default=Path("/harbor-robodojo/results"))
    parser.add_argument("--policy-source", type=Path, default=home / "robodojo" / "direct" / "policy")
    parser.add_argument("--exit-when-done", action="store_true",
                        help="Exit instead of idling for artifact collection")
    args = parser.parse_args(argv)
    try:
        supervise(args.spec, args.root, args.results_dir, args.policy_source, standalone=args.exit_when_done)
        code = 0
    except (EvaluationError, OSError) as exc:
        print(f"RoboDojo Direct evaluation failed: {exc}", file=sys.stderr, flush=True)
        code = 1
    if not args.exit_when_done:
        while True:
            time.sleep(3600)
    return code


if __name__ == "__main__":
    def stop(signum, frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, stop)
    raise SystemExit(main())
