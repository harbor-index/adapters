#!/usr/bin/env python3
"""Dump the task environment's evidence for the separate verifier.

Runs as a Harbor ``[[verifier.collect]]`` hook in this sidecar after the agent
container has stopped. Writes ``/collected``, which ``task.toml`` declares as
an artifact. Mirrors what the upstream runner fetches after a run
(``/_env/audit``, ``/_env/state``, ``/_env/events``, ``/_env/clock``).
"""
from __future__ import annotations

import json
import os
import shutil
import urllib.request
from pathlib import Path

RUN_DIR = Path(os.environ.get("AUTHZ_RUN_DIR", "/opt/authz/run"))
OUT = Path(os.environ.get("AUTHZ_COLLECT_DIR", "/collected"))
BASE_URL = f"http://127.0.0.1:{os.environ.get('AUTHZ_ENV_PORT', '9100')}"
ENDPOINTS = {
    "audit": "/_env/audit",
    "state_after": "/_env/state",
    "events": "/_env/events",
    "clock": "/_env/clock",
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    token = (RUN_DIR / "grader_token").read_text(encoding="utf-8").strip()
    errors: dict[str, str] = {}
    for name, path in ENDPOINTS.items():
        request = urllib.request.Request(
            f"{BASE_URL}{path}", headers={"Authorization": f"Bearer {token}"}
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - recorded for the verifier
            errors[name] = f"{type(exc).__name__}: {exc}"
            continue
        (OUT / f"{name}.json").write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8"
        )
    before = RUN_DIR / "state_before.json"
    if before.is_file():
        shutil.copy2(before, OUT / "state_before.json")
    (OUT / "collect_errors.json").write_text(json.dumps(errors, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
