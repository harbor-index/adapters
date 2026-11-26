#!/usr/bin/env python3
"""Replay the upstream scripted authorized path for this task (oracle).

``authorized_path.json`` holds the steps of the authorized trajectory that the
authors' own judge check graded (``task_generation/stages/accept/
scripted-path-receipts-and-eval/<task>/deepseek-high/authorized_trajectory.json``;
completion = yes, out-of-authorization = no for all 286 tasks). Service calls,
plain agent replies (sent as ``message_user``) and ``message_user`` go through
the task's MCP gateway like an agent's calls;
file and shell steps run in /workspace. The replay records its steps in the
upstream TrajectoryLog format at /logs/agent/authz_trajectory.json, mapped the
way the upstream OpenHands harness records the same actions.

Standard library only: the agent container has no upstream code.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
MCP_URL = os.environ.get("AUTHZ_MCP_URL", "http://authz-env:8000/mcp")
OUT = Path(os.environ.get("AUTHZ_TRAJECTORY_OUT", "/logs/agent/authz_trajectory.json"))
# Where /workspace is on this machine (only differs in local tests).
WORKSPACE = os.environ.get("AUTHZ_WORKSPACE", "/workspace")


def _local(text: str) -> str:
    return text if WORKSPACE == "/workspace" else text.replace("/workspace", WORKSPACE)
OUTPUT_CAP = 20_000


class McpClient:
    """Minimal MCP streamable-HTTP client (initialize + tools/call)."""

    def __init__(self, url: str):
        self.url = url
        self.session: str | None = None
        self._id = 0

    def _post(self, payload: dict) -> dict | None:
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        request = urllib.request.Request(
            self.url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
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
                raise RuntimeError(f"no response for request {payload['id']}")
            return json.loads(body)

    def request(self, method: str, params: dict) -> dict:
        self._id += 1
        message = self._post({"jsonrpc": "2.0", "id": self._id, "method": method,
                              "params": params})
        if message is None or "error" in message:
            raise RuntimeError(f"MCP {method} failed: {message}")
        return message["result"]

    def initialize(self) -> None:
        self.request("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "authorizationbench-oracle", "version": "1.0"},
        })
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, name: str, arguments: dict) -> str:
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        return "\n".join(
            str(block.get("text") or "")
            for block in result.get("content") or []
            if block.get("type") == "text"
        )


def _json_object(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return {}
    try:
        payload = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


class Recorder:
    def __init__(self, task_id: str):
        self.task_id = task_id
        self.start = time.time()
        self.steps: list[dict] = []

    def add(self, step_type: str, **data) -> None:
        self.steps.append({"step": len(self.steps) + 1, "timestamp": time.time(),
                           "type": step_type, "data": data})

    def save(self) -> None:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        end = time.time()
        OUT.write_text(json.dumps({
            "task_id": self.task_id,
            "model": "oracle",
            "timestamp_start": self.start,
            "timestamp_end": end,
            "duration_seconds": end - self.start,
            "total_steps": len(self.steps),
            "trajectory": self.steps,
            "metadata": {"harness": "authorizationbench-oracle-replay"},
        }, indent=2), encoding="utf-8")


def main() -> int:
    path = json.loads((HERE / "authorized_path.json").read_text(encoding="utf-8"))
    rec = Recorder(path["task_id"])
    rec.add("user_message", content=path["instruction"])
    mcp = McpClient(MCP_URL)
    mcp.initialize()
    failures = 0
    for step in path["steps"]:
        tool, args = step["tool"], step.get("arguments") or {}
        try:
            if tool == "user_message":
                rec.add("user_message", content=step.get("content", ""))
            elif tool == "read_file":
                text = Path(_local(args["path"])).read_text(encoding="utf-8", errors="replace")
                rec.add("file_read", path=args["path"], result=text[:OUTPUT_CAP])
            elif tool == "write_file":
                target = Path(_local(args["path"]))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(args["content"], encoding="utf-8")
                rec.add("file_edit", path=args["path"], result=f"Wrote {args['path']}",
                        command="write", file_text=args["content"],
                        content_truncated=False)
            elif tool == "shell":
                proc = subprocess.run(["bash", "-c", _local(args["command"])], cwd=WORKSPACE,
                                      capture_output=True, text=True, timeout=120)
                rec.add("terminal_command", command=args["command"],
                        output=(proc.stdout + proc.stderr)[:OUTPUT_CAP],
                        exit_code=proc.returncode)
            elif tool == "message_user":
                body = mcp.call("message_user", {"message": args["text"]})
                inner = _json_object(body)
                inner = inner.get("body") if isinstance(inner.get("body"), dict) else inner
                reply = inner.get("reply") if isinstance(inner, dict) else None
                rec.add("ask_user", agent_message=args["text"],
                        oracle_response=reply if isinstance(reply, str) else None,
                        responded=bool(inner.get("responded")) if isinstance(inner, dict) else False)
            else:
                # Recorded as the upstream OpenHands runner records MCP calls:
                # published identifier name, no service prefix.
                published = tool.replace(".", "_")
                body = mcp.call(published, args)
                rec.add("tool_call", tool=published, service="", arguments=args,
                        status=_json_object(body).get("status", ""), result=body)
        except Exception as exc:  # noqa: BLE001 - keep replaying, report at the end
            failures += 1
            print(f"step {step.get('index')} {tool} failed: {exc}", file=sys.stderr)
    rec.save()
    print(f"replayed {len(path['steps'])} steps, {failures} failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
