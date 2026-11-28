from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from . import settings

BASE_ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PYTHONUNBUFFERED": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
}
# Broker ports that per-case jobs call: the Candidate's evaluator broker and the Result judge's role broker.
RELAYED_PORTS = (settings.EVALUATOR_BROKER_PORT, settings.ROLE_BROKER_PORTS["judge"])


def take_providers(*roles: str) -> dict[str, dict[str, str]]:
    """Provider settings for the role brokers; the keys leave the environment of every other process."""
    providers = {role: settings.provider(role) for role in roles}
    for role in ("runtime", "judge", "builder"):
        os.environ.pop(f"AGENTSWE_{role.upper()}_API_KEY", None)
    return providers


def write_secret(directory: Path, name: str, values: dict[str, str], mode: int = 0o600) -> Path:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / name
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
    path.chmod(mode)
    return path


def _stop(process: subprocess.Popen) -> None:
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def _start(stack: contextlib.ExitStack, name: str, command: list[str], env: dict[str, str], cwd: Path,
           port: int, log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    log = stack.enter_context((log_dir / f"{name}.log").open("wb"))
    process = subprocess.Popen(command, env={**BASE_ENV, **env}, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                               start_new_session=True)
    stack.callback(_stop, process)
    deadline = time.monotonic() + 60
    while process.poll() is None and time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://{settings.BROKER_HOST}:{port}/healthz", timeout=2):
                return
        except OSError:
            time.sleep(0.5)
    raise RuntimeError(f"{name} did not become healthy (see {log_dir / f'{name}.log'})")


def role_broker(stack: contextlib.ExitStack, role: str, provider: dict[str, str], token: str, *, bind: str,
                port: int, log_dir: Path) -> None:
    """Upstream's provider broker for one role (agentswe/runners/creation_harbor_v1.start_role_broker)."""
    variable = f"AGENTSWE_{role.upper()}_API_KEY"
    key_file = write_secret(settings.KEYS_DIR, f"{role}.key", {variable: provider["api_key"]})
    ledger = log_dir / "ledger" / role
    ledger.mkdir(parents=True, exist_ok=True)
    # The builder broker resends replays and turns provider 429s into 503s, which Codex retries.
    policy = (["--replay-policy", "resend", "--rate-limit-policy", "as_503"] if role == "builder"
              else ["--normalize-reported-model"])
    command = [
        sys.executable, "-m", "agentswe_broker", "serve", "--role", role,
        "--credential-file", str(key_file), "--credential-var", variable,
        "--wire", provider["wire"], "--base-url", provider["base_url"],
        "--model", provider["model"], "--effort", provider["effort"],
        "--stats-file", str(ledger / f"{role}_stats.json"), "--ledger-dir", str(ledger),
        "--bind", bind, "--port", str(port), *policy,
    ]
    env = {"AGENTSWE_BROKER_CLIENT_TOKEN": token, "AGENTSWE_BROKER_STATS_TOKEN": secrets.token_urlsafe(32)}
    _start(stack, f"{role}-broker", command, env, settings.BROKER_DIR, port, log_dir)


def evaluation_brokers(stack: contextlib.ExitStack, providers: dict[str, dict[str, str]], log_dir: Path) -> Path:
    """Upstream's per-run evaluation brokers: runtime and judge role brokers, and the evaluator broker
    (runners/creation/broker_v2.py) that Candidates call. Returns the judge credential file Eval jobs mount."""
    tokens = {role: secrets.token_urlsafe(32) for role in settings.ROLE_BROKER_PORTS}
    for role, port in settings.ROLE_BROKER_PORTS.items():
        role_broker(stack, role, providers[role], tokens[role], bind=settings.BROKER_HOST, port=port, log_dir=log_dir)
    runtime = providers["runtime"]
    credential = write_secret(settings.KEYS_DIR, "evaluator-broker.env", {"AGENTSWE_RUNTIME_API_KEY": tokens["runtime"]})
    telemetry = log_dir / "telemetry"
    telemetry.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable, str(settings.SHARED_DIR / "broker_v2.py"), "--credential-file", str(credential),
        "--bind", settings.BROKER_HOST, "--port", str(settings.EVALUATOR_BROKER_PORT),
        "--ledger-file", str(telemetry / "calls.jsonl"), "--max-runtime-calls", "0", "--max-runtime-tokens", "0",
        "--max-attempts", "4", "--request-timeout", "560", "--upstream-timeout", "300",
    ]
    env = {
        "AGENTSWE_RUNTIME_RESPONSES_URL": settings.role_url("runtime") + "/responses",
        "AGENTSWE_RUNTIME_MODEL": runtime["model"],
        "AGENTSWE_RUNTIME_EFFORT": runtime["effort"],
        "AGENTSWE_RUNTIME_MODEL_ALIASES": os.environ.get("AGENTSWE_RUNTIME_MODEL_ALIASES") or runtime["model"].split("/")[-1],
        "AGENTSWE_BROKER_JUDGE_EFFORT": providers["judge"]["effort"],
        "AGENTSWE_EVALUATOR_JUDGE_ROLE": "off",
    }
    _start(stack, "evaluator-broker", command, env, settings.SHARED_DIR, settings.EVALUATOR_BROKER_PORT, log_dir)
    # Candidates get placeholders only; the evaluator broker holds the runtime credential.
    write_secret(settings.SECRETS_DIR, "candidate.env",
                 {"GATEWAY_API_KEY": "broker-only-placeholder", "SERPER_TOKEN": "search-placeholder"}, 0o644)
    return write_secret(settings.SECRETS_DIR, "judge.env", {"AGENTSWE_JUDGE_API_KEY": tokens["judge"]})


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while data := await reader.read(65536):
        writer.write(data)
        await writer.drain()
    if writer.can_write_eof():
        writer.write_eof()


async def _relay(port: int, *, expose: bool) -> None:
    """One relayed broker port over a Unix socket in the state volume, which the Builder never mounts."""
    path = settings.WORKER_DIR / f"broker-{port}.sock"

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if expose:
            upstream_reader, upstream_writer = await asyncio.open_connection(settings.BROKER_HOST, port)
        else:
            upstream_reader, upstream_writer = await asyncio.open_unix_connection(str(path))
        try:
            await asyncio.gather(_pipe(reader, upstream_writer), _pipe(upstream_reader, writer))
        finally:
            upstream_writer.close()
            writer.close()

    if expose:
        path.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(handle, path=str(path))
        path.chmod(0o600)
    else:
        server = await asyncio.start_server(handle, settings.BROKER_HOST, port)
    await server.serve_forever()


def serve_relays(*, expose: bool) -> None:
    """In the evaluator (expose=True), serve the brokers on Unix sockets; in the worker, listen on the same local
    ports the per-case jobs were staged with and forward to those sockets."""
    async def serve() -> None:
        await asyncio.gather(*(_relay(port, expose=expose) for port in RELAYED_PORTS))

    threading.Thread(target=asyncio.run, args=(serve(),), daemon=True).start()
