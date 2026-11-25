#!/usr/bin/env python3
"""AuthorizationBench task environment sidecar for Harbor.

Mirrors the upstream production runner (``evaluation/run_eval.py``
``HostTaskEnvironment`` plus ``harness/openhands/bundle.py``):

* the task's mock services and the Oracle User run in the upstream FastAPI
  app, bound to loopback and protected by a per-run grader token
  (``protect_business_routes=True``), so the agent cannot reach them directly;
* an MCP gateway on ``0.0.0.0:8000/mcp`` exposes exactly the task's
  allowlisted tools (``allowlist_from_actions``), with identifier-safe names
  (``ToolNameAdapter``), and forwards each call with the grader token.

The agent container reaches only the MCP gateway. The task contract lives in
``/opt/authz/bundle`` inside this container and never enters the agent's
filesystem. ``collect.py`` dumps the audit and service state after the agent
phase for the separate verifier.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
import urllib.request
from pathlib import Path

BUNDLE = Path(os.environ.get("AUTHZ_BUNDLE_DIR", "/opt/authz/bundle"))
RUN_DIR = Path(os.environ.get("AUTHZ_RUN_DIR", "/opt/authz/run"))
WORKSPACE = os.environ.get("AUTHZ_WORKSPACE", "/workspace")
ENV_HOST = "127.0.0.1"
ENV_PORT = int(os.environ.get("AUTHZ_ENV_PORT", "9100"))
MCP_PORT = int(os.environ.get("AUTHZ_MCP_PORT", "8000"))

# Harbor passes the Oracle User profile in AUTHZ_ORACLE_*; the upstream
# Oracle reads LITELLM_* (see services/multi_server.py:configure_oracle_runtime).
_ORACLE_ENV = {
    "AUTHZ_ORACLE_MODEL": "LITELLM_MODEL",
    "AUTHZ_ORACLE_API_KEY": "LITELLM_API_KEY",
    "AUTHZ_ORACLE_BASE_URL": "LITELLM_BASE_URL",
    "AUTHZ_ORACLE_COMPLETION_KWARGS": "LITELLM_COMPLETION_KWARGS",
}


def _configure_oracle_env() -> None:
    for source, target in _ORACLE_ENV.items():
        value = os.environ.get(source, "").strip()
        if value:
            os.environ[target] = value


def _grader_get(base_url: str, token: str, path: str) -> object:
    request = urllib.request.Request(
        f"{base_url}{path}", headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _service_entries(config: dict) -> list[dict]:
    entries = []
    for entry in config.get("services") or []:
        item = dict(entry)
        fixture = item.get("fixture")
        if fixture and not os.path.isabs(fixture):
            item["fixture"] = str((BUNDLE / fixture).resolve())
        entries.append(item)
    return entries


def main() -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    RUN_DIR.chmod(0o700)
    _configure_oracle_env()
    # Services that read the agent's /workspace (email attachments,
    # docs_portal) resolve it through this variable; the shared volume is
    # mounted at the same path here.
    os.environ["SANDBOX_WORKSPACE_HOST_DIR"] = WORKSPACE

    import uvicorn
    from services.multi_server import configure_oracle_runtime, create_app

    from authorization_eva_sandbox.evaluation.harness.mcp_gateway import (
        build_mcp_server,
    )
    from authorization_eva_sandbox.evaluation.harness.tool_name_adapter import (
        ToolNameAdapter,
    )

    config_path = BUNDLE / "task_config.json"
    configure_oracle_runtime(str(config_path))
    config = json.loads(config_path.read_text(encoding="utf-8"))

    token = secrets.token_urlsafe(32)
    token_path = RUN_DIR / "grader_token"
    token_path.write_text(token, encoding="utf-8")
    token_path.chmod(0o600)

    app = create_app(
        _service_entries(config),
        config.get("environment_events") or [],
        port=ENV_PORT,
        reference_time=config.get("reference_time") or None,
        grader_token=token,
        protect_business_routes=True,
    )
    openapi = app.openapi()

    env_server = uvicorn.Server(
        uvicorn.Config(app, host=ENV_HOST, port=ENV_PORT, log_level="warning")
    )
    threading.Thread(target=env_server.run, daemon=True).start()
    deadline = time.monotonic() + 60
    while not env_server.started:
        if time.monotonic() > deadline:
            raise TimeoutError("environment API did not start")
        time.sleep(0.05)

    base_url = f"http://{ENV_HOST}:{ENV_PORT}"
    # Baseline service state for the verifier's service-state diff.
    (RUN_DIR / "state_before.json").write_text(
        json.dumps(_grader_get(base_url, token, "/_env/state"), indent=2, default=str),
        encoding="utf-8",
    )

    names = json.loads((BUNDLE / "mcp_allowlist.json").read_text(encoding="utf-8"))
    mcp = build_mcp_server(
        base_url,
        openapi,
        names,
        grader_token=token,
        tool_names=ToolNameAdapter(names),
    )
    (RUN_DIR / "ready").write_text("ok", encoding="utf-8")
    uvicorn.run(
        mcp.http_app(path="/mcp", transport="http"),
        host="0.0.0.0",
        port=MCP_PORT,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
