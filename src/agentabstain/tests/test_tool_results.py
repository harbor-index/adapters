import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

from fastmcp import Client, FastMCP
from fastmcp.exceptions import ValidationError
from fastmcp.tools.base import _convert_to_content


SPEC = importlib.util.spec_from_file_location(
    "runtime_base", Path(__file__).resolve().parents[1] / "src/agentabstain/assets/runtime/base.py"
)
runtime_base = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime_base)


class ToolResultTests(unittest.TestCase):
    def test_invalid_argument_type_keeps_the_native_mcp_error(self):
        from agentabstain.sdk_worker import public_tool_result

        server = FastMCP("invalid-input")

        @server.tool
        def lookup(account: str) -> str:
            return account

        async def native_reply():
            async with Client(server) as client:
                return await client.call_tool_mcp("lookup", {"account": []})

        native = asyncio.run(native_reply())
        with self.assertRaises(ValidationError) as error:
            runtime_base.BaseEnvironment.call_tool(SimpleNamespace(mcp=server, _broken_tools={}), "lookup", account=[])
        harbor = public_tool_result("test.lookup", {"ok": False, "error": str(error.exception)})
        self.assertEqual(harbor.content, native.content)

    def test_sidecar_returns_the_records_visible_through_mcp(self):
        server = FastMCP("test-results")
        records = [{"id": "A", "name": "Café"}, {"id": "B", "name": "North"}]

        @server.tool
        def lookup(empty: bool) -> list:
            return [] if empty else records

        @server.tool
        def typed_lookup() -> list[dict]:
            return records

        @server.tool
        def detail() -> dict:
            return records[0]

        env = SimpleNamespace(mcp=server, _broken_tools={})
        for name, args, expected in (("lookup", {"empty": False}, records), ("lookup", {"empty": True}, []),
                                     ("typed_lookup", {}, records), ("detail", {}, records[0])):
            with self.subTest(tool=name, args=args):
                native = asyncio.run(server.call_tool(name, args))
                actual = runtime_base.BaseEnvironment.call_tool(env, name, **args)
                self.assertEqual(actual, expected)
                self.assertEqual(_convert_to_content(actual), native.content)
