from __future__ import annotations

import importlib
import json
import os
import pwd
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import settings

# Large read-only trees (environment prefixes, evaluator bundles) are linked, not copied.
LINKED_MOUNT_PATTERN = re.compile(
    r"^/opt/agentswe/benchmark/(?:agent-create-0804/)?envs/[^/]+$"
    r"|^/opt/agentswe-trusted-runtime$|^/opt/agentswe-trusted-browser$"
)
MOUNT_ROOTS = (
    "/tests",
    "/solution",
    "/submission",
    "/active-case",
    "/candidate-output",
    "/evaluator",
    "/harness",
    "/trusted-evidence",
    "/run/secrets",
    "/opt/agentswe",
    "/opt/agentswe-trusted-runtime",
    "/opt/agentswe-trusted-browser",
    "/tools",
)
HARBOR_DIRS = ("/solution", "/tests", "/logs/agent", "/logs/verifier", "/logs/artifacts")
BASE_ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PYTHONUNBUFFERED": "1",
}


class InfrastructureError(RuntimeError):
    """Evaluator-side failure: the affected score is withheld, never set to zero."""


def upstream_adapter():
    """Import the task's upstream adapter module exactly as one_stop.py does."""
    for path in (settings.TASK_DIR / "adapter", settings.SHARED_DIR):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    return importlib.import_module("adapter")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _check_target(target: str) -> None:
    if not any(target == root or target.startswith(root + "/") for root in MOUNT_ROOTS):
        raise InfrastructureError(f"unsupported mount target in upstream compose: {target}")


def materialize_mounts(volumes: list[dict[str, Any]]) -> list[Path]:
    """Recreate upstream bind mounts as copies (data) or symlinks (environment prefixes)."""
    created: list[Path] = []
    for volume in volumes:
        if not isinstance(volume, dict) or volume.get("type", "bind") != "bind":
            continue
        source = Path(str(volume["source"]))
        target = str(volume["target"])
        if source.resolve() == Path(target).resolve():
            continue
        if target.startswith(("/usr/", "/etc/", "/lib")):
            if not Path(target).exists():
                raise InfrastructureError(f"host tool missing in evaluator image: {target}")
            continue
        _check_target(target)
        if not source.exists():
            raise InfrastructureError(f"mount source missing: {source}")
        destination = Path(target)
        _remove(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if LINKED_MOUNT_PATTERN.match(target):
            destination.symlink_to(source.resolve(), target_is_directory=True)
        elif source.is_dir():
            shutil.copytree(source, destination, symlinks=True)
        else:
            shutil.copy2(source, destination)
        created.append(destination)
    return created


def clear_dir(path: Path) -> None:
    """Empty a directory in place: the outer Harbor verifier mounts /logs/verifier."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        _remove(child)


def replace_dir(source: Path, destination: Path) -> None:
    clear_dir(destination)
    shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)


def reset_harbor_dirs() -> None:
    for name in HARBOR_DIRS:
        path = Path(name)
        clear_dir(path)
        path.chmod(0o777)


def service_spec(compose_file: Path) -> tuple[dict[str, str], list[dict[str, Any]]]:
    if not compose_file.is_file():
        return {}, []
    config = read_json(compose_file)
    services = config.get("services", {})
    extra = sorted(set(services) - {"main"})
    if extra:
        raise InfrastructureError(f"upstream compose needs extra services {extra}: not supported in this release")
    main = services.get("main", {})
    environment = main.get("environment", {}) or {}
    if isinstance(environment, list):
        environment = dict(item.split("=", 1) for item in environment)
    return {str(k): str(v) for k, v in environment.items()}, list(main.get("volumes", []) or [])


def _demote(user: str):
    record = pwd.getpwnam(user)

    def apply() -> None:
        os.setgroups([])
        os.setgid(record.pw_gid)
        os.setuid(record.pw_uid)

    return apply


def run_script(script: str, environment: dict[str, str], timeout: float, log: Path, user: str | None = None) -> int:
    env = dict(BASE_ENV)
    env["HOME"] = pwd.getpwnam(user).pw_dir if user else "/root"
    env.update(environment)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("wb") as handle:
        try:
            completed = subprocess.run(
                ["bash", script],
                env=env,
                cwd="/workspace" if Path("/workspace").is_dir() else "/",
                stdout=handle,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                preexec_fn=_demote(user) if user else None,
                start_new_session=True,
            )
        except subprocess.TimeoutExpired:
            return 124
    return completed.returncode


def archive(destination: Path) -> None:
    for name in ("/logs/artifacts", "/logs/verifier"):
        source = Path(name)
        if source.exists():
            shutil.copytree(source, destination / source.name, symlinks=True, dirs_exist_ok=True)


def wait_healthy(process: subprocess.Popen, name: str, port: int) -> None:
    deadline = time.time() + 60
    while time.time() < deadline:
        if process.poll() is not None:
            raise InfrastructureError(f"{name} exited during startup (see {name}.log)")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2):
                return
        except OSError:
            time.sleep(0.5)
    raise InfrastructureError(f"{name} did not become healthy")


def start_role_broker(role: str, provider: dict[str, str], client_token: str, *, bind: str, port: int,
                      log_dir: Path, extra_args: list[str]) -> subprocess.Popen:
    """One provider broker per role (upstream creation_harbor_v1.start_role_broker), as a local process."""
    ledger = log_dir / "ledger" / role
    ledger.mkdir(parents=True, exist_ok=True)
    variable = f"AGENTSWE_{role.upper()}_API_KEY"
    key_file = settings.secret_file(f"{role}.key", {variable: provider["api_key"]})
    command = [
        sys.executable, "-m", "agentswe_broker", "serve", "--role", role,
        "--credential-file", str(key_file), "--credential-var", variable,
        "--wire", provider["wire"], "--base-url", provider["base_url"],
        "--model", provider["model"], "--effort", provider["effort"] or "none",
        "--stats-file", str(ledger / f"{role}_stats.json"), "--ledger-dir", str(ledger),
        "--bind", bind, "--port", str(port), *extra_args,
    ]
    env = {
        **BASE_ENV,
        "PYTHONDONTWRITEBYTECODE": "1",
        "AGENTSWE_BROKER_CLIENT_TOKEN": client_token,
        "AGENTSWE_BROKER_STATS_TOKEN": secrets.token_urlsafe(32),
    }
    log = (log_dir / f"{role}-broker.log").open("wb")
    process = subprocess.Popen(command, env=env, cwd=settings.PROVIDER_BROKER_DIR, stdout=log,
                               stderr=subprocess.STDOUT, start_new_session=True)
    wait_healthy(process, f"{role}-broker", port)
    return process


class Brokers:
    """Upstream's per-run broker stack as local processes (agentswe/runners/creation_harbor_v1.py).

    Runtime and judge role brokers (broker/agentswe_broker) hold the provider keys and pin the
    model and effort; the evaluator broker (runners/creation/broker_v2.py) serves the Candidate
    and forwards to the runtime role broker with a per-run token.
    """

    def __init__(self, phase_dir: Path) -> None:
        self.phase_dir = phase_dir
        self.processes: list[subprocess.Popen] = []
        self.judge_credential: Path | None = None

    def __enter__(self) -> "Brokers":
        tokens = {role: secrets.token_urlsafe(32) for role in settings.ROLE_BROKER_PORTS}
        try:
            for role, token in tokens.items():
                self.processes.append(start_role_broker(
                    role, settings.PROVIDERS[role], token, bind=settings.BROKER_HOST,
                    port=settings.ROLE_BROKER_PORTS[role], log_dir=self.phase_dir,
                    extra_args=["--normalize-reported-model"],
                ))
            self.judge_credential = settings.secret_file("judge.env", {"AGENTSWE_JUDGE_API_KEY": tokens["judge"]})
            runtime_token = settings.secret_file("evaluator-broker.env", {"AGENTSWE_RUNTIME_API_KEY": tokens["runtime"]})
            telemetry = self.phase_dir / "telemetry"
            telemetry.mkdir(parents=True, exist_ok=True)
            log = (self.phase_dir / "evaluator-broker.log").open("wb")
            process = subprocess.Popen([
                sys.executable, str(settings.SHARED_DIR / "broker_v2.py"),
                "--credential-file", str(runtime_token),
                "--bind", settings.BROKER_HOST, "--port", str(settings.BROKER_PORT),
                "--ledger-file", str(telemetry / "calls.jsonl"),
                "--max-runtime-calls", "0", "--max-runtime-tokens", "0",
                "--max-attempts", "4", "--request-timeout", "560", "--upstream-timeout", "300",
            ], env={**BASE_ENV, **settings.evaluator_broker_environment()}, cwd=settings.SHARED_DIR,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            self.processes.append(process)
            wait_healthy(process, "evaluator-broker", settings.BROKER_PORT)
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *exc_info: object) -> None:
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
        self.processes.clear()

    def events(self, evaluation_id: str) -> list[dict[str, Any]]:
        url = (
            f"http://{settings.BROKER_HOST}:{settings.BROKER_PORT}/events?evaluation_id="
            + urllib.parse.quote(evaluation_id, safe="")
        )
        request = urllib.request.Request(url, headers={"Authorization": "Bearer stats-only-placeholder"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.load(response)
        except Exception as exc:
            raise InfrastructureError(f"candidate broker telemetry unavailable: {type(exc).__name__}") from None


def withheld_cases(events: list[dict[str, Any]]) -> list[str]:
    """Upstream policy (runtime_contract.assert_runtime_health): broker/provider failures withhold a case."""
    kinds: dict[str, set[str]] = {}
    for event in events:
        kind = event.get("classification")
        if kind in {"provider_failure", "transport_failure", "broker_failure", "delivery_failure"}:
            kinds.setdefault(str(event.get("case_id")), set()).add(str(kind))
    return sorted(case for case, found in kinds.items() if found != {"delivery_failure"})


def _staging_kwargs(function, available: dict[str, Any]) -> dict[str, Any]:
    import inspect

    parameters = inspect.signature(function).parameters
    missing = [
        name for name, parameter in parameters.items()
        if parameter.default is inspect.Parameter.empty and name not in available
    ]
    if missing:
        raise InfrastructureError(f"{function.__name__} needs unsupported inputs: {missing}")
    return {name: value for name, value in available.items() if name in parameters}


def run_case(
    adapter, *, case_id: str, candidate: Path, candidate_digest: str, phase_dir: Path, credential_file: Path,
    judge_credential: Path,
) -> dict[str, Any]:
    """Candidate job then Eval job for one case, as staged by upstream adapter.py."""
    case_dir = phase_dir / "cases" / case_id
    benchmark = settings.TASK_DIR / "benchmark"
    env_prefix = settings.CANDIDATE_ENV_PREFIX

    # Candidate job (upstream task-template): agent phase runs solution/solve.sh.
    task_dir = adapter.stage_task(**_staging_kwargs(adapter.stage_task, {
        "case_id": case_id,
        "benchmark": benchmark,
        "candidate": candidate,
        "env_prefix": env_prefix,
        "candidate_digest": candidate_digest,
        # Upstream derives the broker evaluation id from <phase>/tasks/<case>.
        "tasks_dir": phase_dir / "tasks",
        "credential_file": credential_file,
    }))
    reset_harbor_dirs()
    replace_dir(task_dir / "solution", Path("/solution"))
    environment, volumes = service_spec(task_dir / "environment" / "docker-compose.yaml")
    materialize_mounts(volumes)
    for name in ("/logs/artifacts", "/logs/agent"):
        shutil.chown(name, user=settings.CANDIDATE_USER, group=settings.CANDIDATE_USER)
    run_script("/solution/solve.sh", environment, settings.CANDIDATE_JOB_TIMEOUT,
               case_dir / "candidate_solve.log", user=settings.CANDIDATE_USER)

    # Candidate verifier (separate environment upstream): tests/test.sh scores the run.
    verifier_env, verifier_volumes = service_spec(task_dir / "tests" / "docker-compose.yaml")
    for mount in ("/submission", "/opt/agentswe/benchmark/envs/.env"):
        _remove(Path(mount))
    replace_dir(task_dir / "tests", Path("/tests"))
    materialize_mounts(verifier_volumes)
    code = run_script("/tests/test.sh", verifier_env, settings.VERIFIER_JOB_TIMEOUT, case_dir / "candidate_test.log")
    archive(case_dir / "candidate_job")
    contract_path = Path("/logs/verifier/score_contract.json")
    if not contract_path.is_file():
        raise InfrastructureError(f"{case_id}: candidate verifier wrote no score contract (exit {code})")
    contract = read_json(contract_path)
    output = Path("/logs/artifacts/candidate_output")
    if contract.get("candidate_digest") != candidate_digest or contract.get("output_digest") != adapter.tree_digest(output):
        raise InfrastructureError(f"{case_id}: candidate execution contract does not match the run")
    staged_output = case_dir / "eval_input" / "candidate_output"
    shutil.copytree(output, staged_output, symlinks=True)
    # Tasks whose Candidate verifier renders trusted evidence hand it to the Eval job
    # (upstream adapter.trusted_evidence_path on the verifier directory).
    trusted_evidence_root = None
    if hasattr(adapter, "trusted_evidence_path"):
        trusted_evidence_root = adapter.trusted_evidence_path(case_dir / "candidate_job" / "verifier", contract)

    # Eval job (upstream eval-template): agent phase is the Result judge.
    eval_dir = adapter.stage_eval_task(**_staging_kwargs(adapter.stage_eval_task, {
        "trusted_evidence_root": trusted_evidence_root,
        "case_id": case_id,
        "benchmark": benchmark,
        "candidate_output": staged_output,
        "candidate_digest": candidate_digest,
        "candidate_output_digest": adapter.tree_digest(staged_output),
        "env_prefix": env_prefix,
        "credential_file": judge_credential,
        "eval_tasks_dir": phase_dir / "eval_tasks",
        "candidate_trial": f"{case_id}__agentswe-harbor",
        "candidate_execution_contract": contract,
    }))
    reset_harbor_dirs()
    replace_dir(eval_dir / "solution", Path("/solution"))
    environment, volumes = service_spec(eval_dir / "environment" / "docker-compose.yaml")
    materialize_mounts(volumes)
    run_script("/solution/solve.sh", environment, settings.EVAL_JOB_TIMEOUT, case_dir / "eval_solve.log")
    verifier_env, verifier_volumes = service_spec(eval_dir / "tests" / "docker-compose.yaml")
    _remove(Path("/run/secrets/agentswe.env"))
    replace_dir(eval_dir / "tests", Path("/tests"))
    materialize_mounts(verifier_volumes)
    code = run_script("/tests/test.sh", verifier_env, settings.VERIFIER_JOB_TIMEOUT, case_dir / "eval_test.log")
    archive(case_dir / "eval_job")
    contract_path = case_dir / "eval_job" / "verifier" / "score_contract.json"
    eval_path = case_dir / "eval_job" / "artifacts" / "eval" / "eval_result.json"
    reward_path = case_dir / "eval_job" / "verifier" / "reward.json"
    if not contract_path.is_file() or not eval_path.is_file():
        raise InfrastructureError(f"{case_id}: eval evidence is incomplete (exit {code})")
    contract = read_json(contract_path)
    evaluation = read_json(eval_path)
    if contract.get("case_id") != case_id or contract.get("candidate_digest") != candidate_digest:
        raise InfrastructureError(f"{case_id}: eval contract does not match the run")
    if contract.get("infrastructure_error") or not reward_path.is_file():
        raise InfrastructureError(f"{case_id}: eval infrastructure error: {contract.get('infrastructure_error')}")
    # Field set of upstream adapter.collect_eval_results.
    return {
        "case_id": case_id,
        "score": int(contract.get("score", 0)),
        "reward": float(read_json(reward_path)["reward"]),
        "validity_gate": bool(contract.get("validity_gate")),
        "contract_valid": bool(contract.get("contract_valid")),
        "provider_counts": contract.get("provider_counts", {}),
        "eval_result": str(eval_path),
        "score_contract": str(contract_path),
        "eval_errors": evaluation.get("errors", []),
    }


def run_phase(phase_id: str, candidate: Path, cases: list[str]) -> tuple[dict[str, Any], Path]:
    """Evaluate one frozen candidate on ``cases``; returns (score_summary, phase_dir)."""
    adapter = upstream_adapter()
    phase_dir = settings.STATE_DIR / "evaluations" / phase_id
    if phase_dir.exists():
        shutil.rmtree(phase_dir)
    phase_dir.mkdir(parents=True)
    candidate_digest = adapter.tree_digest(candidate)
    credential_file = settings.candidate_credential_file()
    results: list[dict[str, Any]] = []
    with Brokers(phase_dir) as brokers:
        for case_id in cases:
            results.append(run_case(
                adapter,
                case_id=case_id,
                candidate=candidate,
                candidate_digest=candidate_digest,
                phase_dir=phase_dir,
                credential_file=credential_file,
                judge_credential=brokers.judge_credential,
            ))
        events = brokers.events(phase_id)
    write_json(phase_dir / "candidate_api_events.json", events)
    withheld = withheld_cases(events)
    if withheld:
        write_json(phase_dir / "infra_failed_cases.json", withheld)
        raise InfrastructureError(f"runtime provider failures withheld cases {withheld}")
    total = sum(item["score"] for item in results)
    summary = {
        "evaluation_mode": getattr(adapter, "EVALUATION_MODE", None),
        "candidate_digest": candidate_digest,
        "cases": results,
        "valid_case_count": sum(1 for item in results if item["validity_gate"]),
        "contract_valid_count": sum(1 for item in results if item["contract_valid"]),
        "total_score": total,
        "max_total_score": 100 * len(results),
        "mean_score": round(total / len(results), 1),
        "mean_reward": round(sum(item["reward"] for item in results) / len(results), 6),
        "api_totals": {
            key: sum(int(item["provider_counts"].get(key, 0)) for item in results)
            for key in ("gateway_text", "gateway_image", "serper", "web_retrieval")
        },
    }
    write_json(phase_dir / "score_summary.json", summary)
    return summary, phase_dir
