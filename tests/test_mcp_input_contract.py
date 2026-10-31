import copy
import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from xuloom.protocols.mcp_bridge import MCP_AVAILABLE, MCPToolWrapper
from xuloom.protocols.dsml import ToolProtocolError
import tests.test_tool_recovery as recovery

SCHEMA = {"type": "object", "properties": {"items": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/item"}}},
    "required": ["items"], "additionalProperties": False,
    "$defs": {"item": {"type": "object", "properties": {"count": {"type": "integer", "minimum": 1, "maximum": 10}}, "required": ["count"], "additionalProperties": False}}}


@unittest.skipUnless(MCP_AVAILABLE, "Optional MCP SDK unavailable")
class MCPInputContractTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = recovery.RecoveryTests.asyncSetUp
    asyncTearDown = recovery.RecoveryTests.asyncTearDown

    def wrapper(self, schema=None):
        session = NS(call_tool=AsyncMock(return_value=NS(content=[NS(type="text", text="saved")], isError=False)))
        wrapper = MCPToolWrapper("fixture", session, NS(name="store", description="store fixture", inputSchema=copy.deepcopy(SCHEMA if schema is None else schema)))
        return wrapper, session

    async def test_nested_invalid_input_is_rejected_before_approval_or_remote_side_effect(self):
        tool, server = self.wrapper()
        self.engine.available_tools[tool.name] = tool
        approve = AsyncMock(side_effect=AssertionError("Invalid input must not prompt for approval"))
        with patch.object(self.engine.permission_manager, "authorize", approve):
            for index, args in enumerate([{}, {"items": []}, {"items": [{"count": 15}]}, {"items": [{"count": "secret-password-value"}]}, {"items": [{"count": True}]}, {"items": [{"count": 2}], "extra": True}]):
                result = await self.engine._execute_tool_wrapper({"id": "invalid-" + str(index), "name": tool.name, "input": args}, lambda _: None)
                self.assertTrue(result["is_error"])
                self.assertTrue(result.get("_input_error"))
                self.assertIn("invalid_tool_input", result["content"])
                self.assertNotIn("secret-password-value", result["content"])
        server.call_tool.assert_not_awaited(); approve.assert_not_awaited()

    async def test_valid_input_still_requires_authorization_and_preserves_values(self):
        tool, server = self.wrapper()
        self.engine.available_tools[tool.name] = tool
        args = {"items": [{"count": 4}]}
        self.engine.permission_manager.approval_policy.set_mode("full")
        result = await self.engine._execute_tool_wrapper({"id": "valid", "name": tool.name, "input": args}, lambda _: None)
        self.assertFalse(result["is_error"])
        server.call_tool.assert_awaited_once_with("store", arguments=args)
        self.engine.permission_manager.always_deny_tools.add(tool.name)
        denied = await self.engine._execute_tool_wrapper({"id": "denied", "name": tool.name, "input": args}, lambda _: None)
        self.assertTrue(denied["is_error"]); self.assertEqual(server.call_tool.await_count, 1)

    async def test_direct_calls_cannot_bypass_validation_and_schema_is_immutable(self):
        tool, server = self.wrapper()
        exported = tool.get_tool_definition(); exported["input_schema"].clear()
        with self.assertRaises(ToolProtocolError): await tool(items=[{"count": 999}])
        server.call_tool.assert_not_awaited()
        self.assertEqual(tool.get_tool_definition()["input_schema"], SCHEMA)

    async def test_external_references_never_trigger_network_or_remote_execution(self):
        tool, server = self.wrapper({"$ref": "https://example.invalid/schema.json"})
        with patch("socket.getaddrinfo", side_effect=AssertionError("Schema validation must stay offline")):
            with self.assertRaisesRegex(ToolProtocolError, "self-contained"):
                await tool(token="secret-password-value")
        server.call_tool.assert_not_awaited()

    async def test_invalid_schema_is_rejected_on_registration(self):
        from jsonschema.exceptions import SchemaError
        with self.assertRaises(SchemaError): self.wrapper({"type": "not-a-json-schema-type"})
        with self.assertRaisesRegex(ValueError, "Unsupported MCP JSON Schema dialect"):
            self.wrapper({"$schema": "https://example.invalid/unsupported-schema"})

    async def test_model_repair_uses_existing_loop_without_repeating_remote_work(self):
        tool, server = self.wrapper()
        self.engine.available_tools[tool.name] = tool
        self.engine.permission_manager.approval_policy.set_mode("full")
        recovery.RecoveryTests.fixture(self, [(tool.name, json.dumps({"items": [{"count": 15}]})),
            (tool.name, json.dumps({"items": [{"count": 5}]})), "REPAIRED"])
        result = await self.engine.run("Call the fixture with valid arguments.", sys_print_callback=lambda _: None)
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "REPAIRED")
        self.assertEqual(len(self.requests), 3)
        server.call_tool.assert_awaited_once_with("store", arguments={"items": [{"count": 5}]})
