
import unittest
from typing import Annotated, List, Literal, Optional

from pydantic import Field

from xuloom import PluginAPI, tool
from xuloom.models import ToolCall, model_response
from xuloom.tools.base import BaseAgenticTool
from xuloom.tools.function import function_tool

from tests.test_model_adapters import AdapterFixture


@tool(read_only=True)
async def lookup_ticket(ticket_id: str, include_history: bool = False) -> dict:
    """Return the status of an internal ticket.

    Args:
        ticket_id: Ticket identifier such as "T-123".
        include_history: Also return past
            status changes.

    Returns:
        The ticket record.
    """
    return {"id": ticket_id, "status": "open", "history": ["new"] if include_history else []}


@tool(name="word-count")
def count_words(text: str, mode: Literal["words", "chars"] = "words",
                limit: Annotated[int, Field(ge=1, le=100)] = 10, context=None) -> str:
    """Count words or characters."""
    value = len(text.split()) if mode == "words" else len(text)
    return f"{min(value, limit)} in {context.get('session_cwd') is not None}"


class SchemaTests(unittest.TestCase):
    def test_schema_description_and_defaults_come_from_the_function(self):
        self.assertTrue(isinstance(lookup_ticket, type) and issubclass(lookup_ticket, BaseAgenticTool))
        definition = lookup_ticket.get_tool_definition()
        self.assertEqual(definition["name"], "lookup_ticket")
        self.assertEqual(definition["description"], "Return the status of an internal ticket.")
        properties = definition["input_schema"]["properties"]
        self.assertEqual(definition["input_schema"]["required"], ["ticket_id"])
        self.assertEqual(properties["ticket_id"]["description"], 'Ticket identifier such as "T-123".')
        self.assertEqual(properties["include_history"]["description"], "Also return past status changes.")
        self.assertIs(properties["include_history"]["default"], False)
        self.assertEqual((lookup_ticket.is_read_only, lookup_ticket.risk_level), (True, "low"))

    def test_context_is_injected_not_exposed_and_constraints_are_kept(self):
        schema = count_words.get_tool_definition()["input_schema"]
        self.assertEqual(sorted(schema["properties"]), ["limit", "mode", "text"])
        self.assertEqual(schema["properties"]["mode"]["enum"], ["words", "chars"])
        self.assertEqual((schema["properties"]["limit"]["minimum"], schema["properties"]["limit"]["maximum"]), (1, 100))
        self.assertEqual((count_words.name, count_words.risk_level), ("word-count", "high"))

    def test_invalid_definitions_are_rejected(self):
        def undocumented(x: int):
            return x
        with self.assertRaisesRegex(ValueError, "needs a description"):
            function_tool(undocumented)
        function_tool(undocumented, description="ok")  # an explicit description is enough

        def variadic(*items: str):
            """Many items."""
        with self.assertRaises(TypeError):
            function_tool(variadic)
        with self.assertRaisesRegex(ValueError, "Invalid tool name"):
            function_tool(lambda: None, description="x")  # "<lambda>"
        with self.assertRaises(ValueError):
            function_tool(undocumented, description="x", risk_level="extreme")

    def test_optional_and_list_types(self):
        @tool
        def tag(names: List[str], note: Optional[str] = None) -> List[str]:
            """Tag things."""
            return names
        schema = tag.get_tool_definition()["input_schema"]
        self.assertEqual(schema["properties"]["names"]["type"], "array")
        self.assertEqual(schema["required"], ["names"])


class EngineTests(AdapterFixture):
    async def test_decorated_tools_run_in_the_normal_pipeline_with_validation(self):
        self.adapter.script = [
            model_response(tool_calls=[ToolCall("1", "lookup_ticket", {"ticket_id": "T-1", "include_history": True}),
                                       ToolCall("2", "word-count", {"text": "a b c", "limit": 0})]),
            model_response(text="done"),
        ]
        async with self.engine(extra_tools=(lookup_ticket, count_words)) as engine:
            result = await engine.run("look it up", sys_print_callback=lambda *_: None)
            self.assertTrue(result.ok, result)
        results = {block["tool_use_id"]: block for message in self.adapter.requests[1].messages
                   for block in message.get("content", []) if isinstance(block, dict) and block.get("type") == "tool_result"}
        self.assertFalse(results["1"]["is_error"])
        self.assertIn('"history": ["new"]', str(results["1"]["content"]))  # dict result sent as JSON
        self.assertTrue(results["2"]["is_error"])  # limit=0 violates ge=1 and is rejected before running
        self.assertIn("Invalid tool input", str(results["2"]["content"]))

    async def test_each_engine_gets_its_own_instance(self):
        async with self.engine(extra_tools=(lookup_ticket,)) as first, self.engine(extra_tools=(lookup_ticket,), cwd=str(self.root)) as second:
            one, two = first.available_tools["lookup_ticket"], second.available_tools["lookup_ticket"]
            self.assertIsNot(one, two)
            self.assertIs(one.context, first.tool_context)
            self.assertIs(two.context, second.tool_context)

    async def test_plugins_can_add_decorated_tools(self):
        def register(api: PluginAPI):
            api.add_tool(count_words, latent=True)
        async with self.engine(plugins=(register,)) as engine:
            self.assertIn("word-count", engine.latent_tools)
        with self.assertRaises(TypeError):
            PluginAPI("x").add_tool(object())


if __name__ == "__main__":
    unittest.main()
