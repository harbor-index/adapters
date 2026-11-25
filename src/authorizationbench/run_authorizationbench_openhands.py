#!/usr/bin/env python3
"""Upstream AuthorizationBench OpenHands 1.49.2 harness, run inside a Harbor task.

This reproduces ``evaluation/harness/openhands/bundle.py::run_openhands_bundle``
(native prompt condition) with Harbor providing the environment:

* the services, Oracle User and allowlisted MCP gateway run in the task's
  ``authz-env`` sidecar (upstream: ``HostTaskEnvironment`` + gateway on the host);
* the agent works in this container's ``/workspace`` (upstream: a DockerWorkspace
  over a copy of ``tasks/<T>/workspace``).

Everything else is upstream code at the pinned commit: the SDK agent with the
task's shell-tool subset and condenser, the native system-prompt suffix, the
35-step cap, the 300 s wall clock, the loop that routes a plain agent reply to
the Oracle User, and ``record_openhands_events`` for the judge-facing
trajectory (written to ``/logs/agent/authz_trajectory.json``).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

MARKER = "</ENVIRONMENT_NOTES>"


class McpClient:
    """Minimal MCP streamable-HTTP client for the Oracle reply route."""

    def __init__(self, url: str):
        self.url, self.session, self._id = url, None, 0

    def _post(self, payload: dict) -> dict | None:
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        request = urllib.request.Request(self.url, data=json.dumps(payload).encode(),
                                         headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=300) as response:
            self.session = response.headers.get("Mcp-Session-Id") or self.session
            body = response.read().decode("utf-8")
            if "id" not in payload:
                return None
            if "text/event-stream" in (response.headers.get("Content-Type") or ""):
                for line in body.splitlines():
                    if line.startswith("data:"):
                        message = json.loads(line[5:].strip())
                        if message.get("id") == payload["id"]:
                            return message
                raise RuntimeError("no MCP response")
            return json.loads(body)

    def request(self, method: str, params: dict) -> dict:
        self._id += 1
        message = self._post({"jsonrpc": "2.0", "id": self._id, "method": method,
                              "params": params})
        if message is None or "error" in message:
            raise RuntimeError(f"MCP {method} failed: {message}")
        return message["result"]

    def initialize(self) -> None:
        self.request("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                    "clientInfo": {"name": "authz-oracle-route",
                                                   "version": "1.0"}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})


def oracle_reply_fn(mcp_url: str):
    """Upstream ``run_eval.oracle_reply_fn``: POST /user/send_message, return the reply.

    Upstream calls the environment API with the grader token; here the same
    route is reached through the sidecar's ``message_user`` tool.
    """
    client = McpClient(mcp_url)
    client.initialize()

    def reply(text: str) -> str | None:
        result = client.request("tools/call", {"name": "message_user",
                                               "arguments": {"message": text}})
        body = "\n".join(str(b.get("text") or "") for b in result.get("content") or []
                         if b.get("type") == "text")
        start, end = body.find("{"), body.rfind("}")
        payload = json.loads(body[start:end + 1]) if 0 <= start < end else {}
        inner = payload.get("body") if isinstance(payload.get("body"), dict) else payload
        value = inner.get("reply") if isinstance(inner, dict) else None
        return value if isinstance(value, str) else None

    return reply


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instruction-file", required=True)
    parser.add_argument("--task-file", default="/opt/authz-agent/task.json")
    parser.add_argument("--mcp-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--reasoning-effort", default=None)
    parser.add_argument("--max-output-tokens", type=int, default=None)
    parser.add_argument("--extra-body-json", default=None)
    parser.add_argument("--task-timeout-sec", type=float, default=300.0)
    parser.add_argument("--out-dir", default="/logs/agent")
    args = parser.parse_args()

    task = json.loads(Path(args.task_file).read_text(encoding="utf-8"))
    home = str(task.get("home") or "/workspace")

    from authorization_eva_sandbox.evaluation.harness.agent_container import (
        sandbox_process_env,
    )
    os.environ.update(sandbox_process_env(home))

    from authorization_eva_sandbox.evaluation.harness.condenser_budget import condenser_budget
    from authorization_eva_sandbox.evaluation.harness.openhands.conversation import (
        native_system_text,
        open_task_conversation,
        shell_tools_for_actions,
    )
    from authorization_eva_sandbox.evaluation.harness.openhands.llm import llm_from_profile
    from authorization_eva_sandbox.evaluation.harness.openhands.runtime import (
        OpenHandsLocalRuntime,
    )
    from authorization_eva_sandbox.evaluation.harness.openhands_events import (
        record_openhands_events,
    )
    from authorization_eva_sandbox.evaluation.harness.prompt_composition import (
        ENVIRONMENT_NOTES,
        NATIVE,
        Composition,
        native_prompt_for,
    )
    from authorization_eva_sandbox.evaluation.run_eval import (
        agent_mcp_config,
        context_management_record,
    )
    from authorization_eva_sandbox.trajectory_logger import TrajectoryLog

    # instruction.md = native_addition(sealed) + "\n" + instruction (see adapter.py).
    text = Path(args.instruction_file).read_text(encoding="utf-8")
    cut = text.index(MARKER) + len(MARKER)
    suffix = text[:cut] + "\n"
    instruction = text[cut:].strip()
    sealed = suffix[: -len("\n\n" + ENVIRONMENT_NOTES + "\n")] + "\n"
    composition = Composition(NATIVE, "openhands", sealed, suffix, native_prompt_for("openhands"))

    profile: dict = {"model": args.model,
                     "api_key": os.environ.get("LLM_API_KEY", ""),
                     "base_url": os.environ.get("LLM_BASE_URL") or None}
    if args.reasoning_effort:
        profile["reasoning_effort"] = args.reasoning_effort
    if args.max_output_tokens:
        profile["max_output_tokens"] = args.max_output_tokens
    if args.extra_body_json:
        profile["completion_kwargs"] = {"extra_body": json.loads(args.extra_body_json)}
    llm = llm_from_profile(profile)

    shell_tools = shell_tools_for_actions(task.get("shell_actions") or [])
    task_config = {"task_id": task["task_id"], "home": home, "agent": task.get("agent") or {}}
    native_text = native_system_text(llm, shell_tools)
    budget = condenser_budget(task_config, condition=NATIVE, native_text=native_text,
                              model=args.model)

    # open_task_conversation reads task_config.json from a bundle directory;
    # only the agent block (step cap, condenser) is used there.
    bundle = Path(tempfile.mkdtemp(prefix="authz-bundle-"))
    (bundle / "task_config.json").write_text(json.dumps(task_config), encoding="utf-8")

    # The SDK client resolves the gateway by URL; agent_mcp_config gives the
    # upstream server name and transport.
    mcp_config = agent_mcp_config(0)
    mcp_config["authorization-bench"]["url"] = args.mcp_url

    started = time.time()
    conversation = open_task_conversation(
        home, bundle, mcp_config, llm=llm, tools=shell_tools,
        condenser_max_tokens=budget["effective_max_tokens"] if budget else None,
        native_suffix=suffix,
    )
    runtime = OpenHandsLocalRuntime(
        conversation, instruction, task=task["task_id"], model=args.model,
        task_timeout_seconds=args.task_timeout_sec,
        user_reply_fn=oracle_reply_fn(args.mcp_url),
        prompt_composition=composition, native_text=native_text, condenser_budget=budget,
    )
    failure = None
    try:
        produced = runtime.run()
    except Exception as exc:  # noqa: BLE001 - keep the partial run, as upstream does
        failure = exc
        produced = getattr(runtime, "partial", None) or {"events": [], "metadata": {}}
    ended = time.time()

    traj = TrajectoryLog(task_id=task["task_id"], model=args.model)
    traj.timestamp_start, traj.timestamp_end = started, ended
    events = list(produced.get("events") or [])
    record_openhands_events(traj, events)  # no tool_names, as in run_single_task
    traj.metadata.update(produced.get("metadata") or {})
    traj.metadata["context_management"] = context_management_record(
        task_config, events, traj.metadata.get("condenser_budget"))
    state = produced.get("state") or {}
    traj.metadata["stop_reason"] = state.get("last_error") or state.get("agent_state")
    if failure is not None:
        traj.metadata["error"] = f"{type(failure).__name__}: {failure}"

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "authz_trajectory.json").write_text(json.dumps(traj.to_dict(), indent=2, default=str),
                                               encoding="utf-8")
    (out / "authz_cost.json").write_text(json.dumps(produced.get("cost_summary") or {},
                                                    indent=2, default=str), encoding="utf-8")
    conversation.close()
    print(f"finished: steps={len(traj.steps)} state={state} error={failure!r}")
    return 1 if failure is not None else 0


if __name__ == "__main__":
    sys.exit(main())
