"""Run the source SDK using the tool catalog and results visible in a Harbor trial."""
import asyncio
import json
import os
import re
import sys

from agents import Agent, ModelSettings, Runner, set_tracing_disabled
from agents.exceptions import UserError
from agents.mcp.server import MCPServer
from fastmcp.tools.base import _convert_to_content
from mcp.types import CallToolResult, ListPromptsResult, TextContent, Tool


def send(payload):
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def public_tool_result(dotted, reply):
    if reply.get("ok") is True:
        return CallToolResult(content=_convert_to_content(reply.get("result")), is_error=False)
    message = str(reply.get("error", ""))
    validation_error = re.match(r"\d+ validation errors? for call\[", message)
    if not validation_error and not message.startswith(("Error calling tool ", "Input validation error")):
        message = f"Error calling tool '{dotted.split('.', 1)[1]}': {message}"
    return CallToolResult(content=[TextContent(type="text", text=message)], is_error=True)


class PublicTools(MCPServer):
    name = "task_env"

    def __init__(self, public):
        super().__init__()
        self.live = {item["name"].replace(".", "__"): item for item in public}
        self.tools = []
        for name, item in self.live.items():
            schema = item["input_schema"]
            # The native MCP wire includes the empty required array for optional arguments.
            native_schema = {
                "type": schema["type"], "properties": schema["properties"],
                "required": schema.get("required", []), **schema,
            }
            self.tools.append(Tool(name=name, description=item.get("description") or "",
                                   input_schema=native_schema))
        self.lock = asyncio.Lock()

    async def connect(self):
        pass

    async def cleanup(self):
        pass

    async def list_tools(self, run_context=None, agent=None):
        return self.tools

    async def list_prompts(self):
        return ListPromptsResult(prompts=[])

    async def get_prompt(self, name, arguments, meta=None):
        raise KeyError(name)

    async def call_tool(self, tool_name, arguments, meta=None):
        item = self.live[tool_name]
        dotted = item["name"]
        args = arguments or {}
        missing = sorted(set(item["input_schema"].get("required", [])) - set(args))
        if missing:
            raise UserError(f"Failed to call tool '{dotted}' on MCP server 'task_env': "
                            f"missing required parameters: {', '.join(missing)}")
        async with self.lock:
            send({"kind": "call", "name": dotted, "args": args})
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                raise RuntimeError("Harbor tool bridge terminated")
            reply = json.loads(line)
        return public_tool_result(dotted, reply)


async def main():
    set_tracing_disabled(True)
    request = json.loads(sys.stdin.readline())
    server = PublicTools(request["tools"])
    actor = Agent(name="task_agent", instructions=request["system"], model=request["model"],
                  model_settings=ModelSettings(temperature=None), mcp_servers=[server])
    result = await Runner.run(starting_agent=actor, input=request["user"], max_turns=30)
    if not isinstance(result.final_output, str) or not result.final_output.strip():
        raise RuntimeError("The SDK did not produce a final response")
    from src.runtime.openaisdk import _extract_usage_metadata

    public_items = [item for item in result.to_input_list() if item.get("type") != "reasoning"]
    send({"kind": "final", "response": result.final_output, "usage": _extract_usage_metadata(result),
          "items": public_items})


if __name__ == "__main__":
    sys.path.insert(0, os.environ["AGENTABSTAIN_SOURCE"])
    asyncio.run(main())
