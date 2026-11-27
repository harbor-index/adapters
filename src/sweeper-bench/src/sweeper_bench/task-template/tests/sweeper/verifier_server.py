#!/usr/bin/env python3
"""Command server for the SWEeper-Bench browser-verifier sidecar.

Runs inside the upstream evaluation-codex image, in the network namespace of
the evaluation product container (`main`). It does two things:

1. Forwards 127.0.0.1:13200 to the product on 127.0.0.1:3000. Upstream
   publishes container port 3000 on VM port 13200 and the verifier reaches it
   with --network host; this forwarder gives the same address.
2. Runs commands that tests/test.sh drops into /sweeper-exchange/rpc/<id>/:
   `argv` (NUL-separated) and optional `env` (KEY=VALUE lines), then `ready`.
   It writes `stdout`, `stderr`, `exit` and finally `done`.

Subcommands used through that channel:
  wait-ready   Probe http://127.0.0.1:13200 like upstream runtime/product.sh.
  summarize    Turn <phase>-result.json into the Harbor verdict and reward.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

EXCHANGE = Path(os.environ.get("SWEEPER_EXCHANGE", "/sweeper-exchange"))
PUBLISH_PORT = int(os.environ.get("PRODUCT_PUBLISH_PORT", "13200"))
CONTAINER_PORT = int(os.environ.get("PRODUCT_CONTAINER_PORT", "3000"))
CATEGORIES = ("target", "preservation")


# --------------------------------------------------------------------------
# 127.0.0.1:13200 -> 127.0.0.1:3000
# --------------------------------------------------------------------------


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for sock, how in ((src, socket.SHUT_RD), (dst, socket.SHUT_WR)):
            try:
                sock.shutdown(how)
            except OSError:
                pass


def _handle(client: socket.socket) -> None:
    try:
        upstream = socket.create_connection(("127.0.0.1", CONTAINER_PORT), timeout=30)
        upstream.settimeout(None)
    except OSError:
        client.close()
        return
    a = threading.Thread(target=_pipe, args=(client, upstream), daemon=True)
    b = threading.Thread(target=_pipe, args=(upstream, client), daemon=True)
    a.start()
    b.start()
    a.join()
    b.join()
    client.close()
    upstream.close()


def forwarder() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", PUBLISH_PORT))
    server.listen(256)
    while True:
        client, _ = server.accept()
        threading.Thread(target=_handle, args=(client,), daemon=True).start()


# --------------------------------------------------------------------------
# Command channel
# --------------------------------------------------------------------------


def _read_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            env[key] = value
    return env


def _serve_one(job: Path) -> None:
    argv = [a for a in (job / "argv").read_bytes().decode().split("\0") if a]
    env = os.environ.copy()
    env.update(_read_env(job / "env"))
    try:
        with (job / "stdout").open("wb") as out, (job / "stderr").open("wb") as err:
            code = subprocess.run(argv, stdout=out, stderr=err, env=env, cwd="/evaluation").returncode
    except Exception as exc:  # report, never kill the server
        (job / "stderr").write_text(f"verifier_server: {type(exc).__name__}: {exc}\n")
        code = 127
    (job / "exit").write_text(str(code))
    (job / "done").touch()


def serve() -> None:
    rpc = EXCHANGE / "rpc"
    rpc.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=forwarder, daemon=True).start()
    (EXCHANGE / "server.ready").write_text(str(os.getpid()))
    print(f"verifier_server: forwarding 127.0.0.1:{PUBLISH_PORT} -> :{CONTAINER_PORT}", flush=True)
    while True:
        for job in sorted(rpc.iterdir()):
            if (job / "ready").is_file() and not (job / "started").exists():
                (job / "started").touch()
                threading.Thread(target=_serve_one, args=(job,), daemon=True).start()
        time.sleep(0.5)


# --------------------------------------------------------------------------
# wait-ready: same probe as upstream runtime/product.sh
# --------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Report a 3xx as-is, like `curl -w %{http_code}` without -L in product.sh."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def wait_ready(timeout: int = 600, probe_timeout: int = 45) -> int:
    url = f"http://127.0.0.1:{PUBLISH_PORT}"
    deadline = time.monotonic() + timeout
    print(f"waiting for {url} (max {timeout}s)", flush=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    while time.monotonic() < deadline:
        status = 0
        try:
            with opener.open(url, timeout=probe_timeout) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        except Exception:
            status = 0
        if status and status < 500:
            print(f"product ready: {url} ({status})", flush=True)
            return 0
        time.sleep(5)
    print(f"product wait timeout: {url}", file=sys.stderr, flush=True)
    return 1


# --------------------------------------------------------------------------
# summarize: upstream verdict cells -> Harbor reward
# --------------------------------------------------------------------------


def _expected(workflows: Path) -> dict[str, list[str]]:
    import yaml

    data = yaml.safe_load(workflows.read_text(encoding="utf-8")) or {}
    out: dict[str, list[str]] = {c: [] for c in CATEGORIES}
    for item in data.get("workflows") or []:
        if item.get("category") in out:
            out[item["category"]].append(str(item.get("id")))
    return out


def summarize(phase: str, result_json: Path, workflows: Path, dest: Path) -> int:
    expected = _expected(workflows)
    data: dict = {}
    if result_json.is_file():
        try:
            data = json.loads(result_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    if data.get("phase") != phase:
        data = {}
    verdict: dict = {"phase": phase}
    complete = True
    for category in CATEGORIES:
        cell = data.get(category)
        ids = expected[category]
        if not isinstance(cell, dict):
            verdict[category] = {"result": "missing", "expected": ids}
            complete = False
            continue
        seen = [str(w.get("id")) for w in cell.get("workflows") or [] if isinstance(w, dict)]
        if sorted(seen) != sorted(ids):
            result = "missing"
            complete = False
        elif cell.get("fail"):
            result = "fail"
        elif cell.get("uncertain"):
            result = "uncertain"
        else:
            result = "pass"
        # Same rule as upstream parse_results.hard_uncertain(): an exception or
        # unparseable verdict is an infrastructure outcome, not a product verdict.
        hard = [
            str(w.get("id"))
            for w in cell.get("workflows") or []
            if isinstance(w, dict) and w.get("result") == "uncertain" and _hard_uncertain(w)
        ]
        verdict[category] = {
            "result": result,
            "pass": cell.get("pass", 0),
            "fail": cell.get("fail", 0),
            "uncertain": cell.get("uncertain", 0),
            "fail_id": cell.get("fail_id", []),
            "uncertain_id": cell.get("uncertain_id", []),
            "infrastructure_uncertain_id": hard,
        }
    infra = any(verdict[c].get("infrastructure_uncertain_id") for c in CATEGORIES)
    verdict["complete"] = complete
    verdict["infrastructure_error"] = infra
    verdict["solved"] = complete and all(verdict[c]["result"] == "pass" for c in CATEGORIES)
    verdict["reward"] = 1 if verdict["solved"] else 0
    dest.write_text(json.dumps(verdict, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(verdict))
    if not complete:
        return 3
    # A real `fail` already decides the task; otherwise a verifier crash must
    # not be reported as an unsolved task.
    if infra and not any(verdict[c]["result"] == "fail" for c in CATEGORIES):
        return 4
    return 0


def _hard_uncertain(item: dict) -> bool:
    agent_result = item.get("agent_result")
    if not isinstance(agent_result, dict):
        return True
    if agent_result.get("parse_error") or agent_result.get("error"):
        return True
    value = agent_result.get("result")
    return not (isinstance(value, str) and value.strip().lower() in {"pass", "fail", "uncertain"})


def main() -> None:
    if len(sys.argv) == 1:
        serve()
        return
    cmd = sys.argv[1]
    if cmd == "wait-ready":
        timeout = int(sys.argv[2]) if len(sys.argv) > 2 else 600
        raise SystemExit(wait_ready(timeout))
    if cmd == "summarize":
        phase, result_json, workflows, dest = sys.argv[2:6]
        raise SystemExit(summarize(phase, Path(result_json), Path(workflows), Path(dest)))
    raise SystemExit(f"unknown command: {cmd}")


if __name__ == "__main__":
    main()
