"""Harbor agent used for the matched AgentAbstain OpenAI Agents SDK experiment."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shlex

from harbor.agents.base import BaseAgent


def public_prompts(instruction: str) -> tuple[str, str]:
    prefix = "## System Prompt\n"
    if not instruction.startswith(prefix):
        raise ValueError("Unrecognized AgentAbstain instruction")
    system, remainder = instruction[len(prefix):].split("\n\n## User Request\n", 1)
    user, _ = remainder.rsplit("\n\n## Available Tools\n", 1)
    return system, user


async def public_call(environment, name=None, args=None):
    command = ["python3", "/app/agentabstain_cli.py"]
    if name is None:
        command.append("--list-tools")
    else:
        command.extend(["--call", name, "--params-json", json.dumps(args, ensure_ascii=False)])
    result = await environment.exec(shlex.join(command), timeout_sec=45)
    try:
        reply = json.loads(result.stdout or "")
    except ValueError as exc:
        raise RuntimeError("The public tool bridge did not return JSON") from exc
    if name is None and (result.return_code != 0 or reply.get("ok") is not True):
        raise RuntimeError("The runtime could not list its public tools")
    transport_errors = ("<urlopen error", "HTTP Error ", "invalid runtime response:")
    if not reply.get("ok") and str(reply.get("error", "")).startswith(transport_errors):
        raise RuntimeError("The runtime transport failed while handling a tool call")
    return reply


class AgentAbstainSDK(BaseAgent):
    @staticmethod
    def name():
        return "agentabstain-openai-agents"

    def version(self):
        return "0.25.0"

    async def setup(self, environment):
        pass

    async def run(self, instruction, environment, context):
        system, user = public_prompts(instruction)
        schemas = (await public_call(environment))["tools"]
        source = Path(os.environ["AGENTABSTAIN_SOURCE"]).resolve()
        worker = Path(__file__).with_name("sdk_worker.py")
        proc = await asyncio.create_subprocess_exec(
            str(source / ".venv/bin/python"), str(worker), cwd=source,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=16 * 1024 * 1024,
        )
        stderr = asyncio.create_task(proc.stderr.read())

        async def send(payload):
            proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
            await proc.stdin.drain()

        final = None
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        try:
            model = self.model_name.removeprefix("openai/")
            await send({"system": system, "user": user, "model": model, "tools": schemas})
            with (self.logs_dir / "public_tool_calls.jsonl").open("w", encoding="utf-8") as log:
                while line := await proc.stdout.readline():
                    item = json.loads(line)
                    if item["kind"] == "final":
                        final = item
                        break
                    if item["kind"] != "call":
                        raise ValueError("Unexpected worker message")
                    reply = await public_call(environment, item["name"], item["args"])
                    log.write(json.dumps({**item, "result": reply}, ensure_ascii=False) + "\n")
                    log.flush()
                    await send(reply)
            proc.stdin.close()
            code = await proc.wait()
            error = (await stderr).decode(errors="replace")
            key = os.environ.get("OPENAI_API_KEY")
            if key:
                error = error.replace(key, "[redacted]")
            error = re.sub(r"sk-[a-zA-Z0-9_-]{12,}", "[redacted]", error)
            if error:
                (self.logs_dir / "sdk-stderr.log").write_text(error, encoding="utf-8")
            if code or final is None:
                raise RuntimeError(f"Native SDK worker did not finish (exit {code}): {error[-300:]}")
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            await stderr

        response = self.logs_dir / "final_response.txt"
        response.write_text(final["response"], encoding="utf-8")
        await environment.upload_file(response, str(self.environment_logs_dir / "final_response.txt"))
        (self.logs_dir / "sdk_transcript.json").write_text(
            json.dumps(final["items"], ensure_ascii=False), encoding="utf-8"
        )
        usage = final.get("usage") or {}
        context.n_input_tokens = usage.get("input_tokens")
        context.n_output_tokens = usage.get("output_tokens")
        context.metadata = {
            "system_sha256": hashlib.sha256(system.encode()).hexdigest(),
            "user_sha256": hashlib.sha256(user.encode()).hexdigest(),
            "model_requests": usage.get("requests"),
        }
