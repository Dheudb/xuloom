"""Discovery visibility survives real history without becoming execution authority."""
from unittest.mock import patch
import unittest
import copy

import test_external_agents as fixtures
from xuloom.agents.role_runtime import configure_role
from xuloom.core.messages import create_user_message
from xuloom.core.tool_execution import restore_tool_catalog
from xuloom.agents.external.program import supports


class ToolCatalogTests(fixtures.ExternalFixture):
    async def test_exact_lookup_and_activation_deliver_existing_tool_contract(self):
        engine = self.engine()
        search = engine.available_tools["tool_search_tool"]
        output = await search.execute(query="command_tool")
        self.assertIn(engine.available_tools["command_tool"].prompt(), output["content"])
        tool = engine.latent_tools["task_list_tool"]
        output = await search.execute(query="tasks", activate=["task_list_tool"])
        self.assertIn("task_list_tool", engine.available_tools)
        if tool.prompt(): self.assertIn(tool.prompt(), output["content"])
        self.assertFalse(self.records)

    async def test_loading_a_different_session_clears_previous_discovery(self):
        engine = self.engine(); engine.require_writer()
        engine.messages.append(create_user_message("A")); self.assertTrue(engine.persist_session_state())
        empty = engine.session_id
        engine.session_manager.release_writer()
        engine.session_id = "activated-session"; engine.require_writer()
        engine.messages = [create_user_message("B")]
        self.assertEqual(engine.activate_tools(["task_list_tool"]), ["task_list_tool"])
        self.assertTrue(engine.persist_session_state())
        activated = engine.session_id
        self.assertTrue(engine.load_session(empty))
        self.assertIn("task_list_tool", engine.latent_tools)
        self.assertNotIn("task_list_tool", engine.available_tools)
        self.assertTrue(engine.load_session(activated))
        self.assertIn("task_list_tool", engine.available_tools)
        self.assertEqual(engine._build_session_metadata()["activated_tools"], ["task_list_tool"])
        await engine.aclose()
        restored = self.engine(); self.assertTrue(restored.load_session(activated))
        self.assertIn("task_list_tool", restored.available_tools)
        self.assertFalse(self.records, "Restoring visibility must not call a model")

    async def test_saved_visibility_cannot_restore_a_tool_removed_by_the_role(self):
        engine = self.engine()
        configure_role(engine, {"disallowed_tools": ["task_list_tool"]}, allowed_tools=None)
        restore_tool_catalog(engine, {"activated_tools": ["task_list_tool", "missing_plugin", "agent_tool"]})
        self.assertEqual(engine.activated_tools, {"missing_plugin"})
        self.assertNotIn("task_list_tool", engine.available_tools | engine.latent_tools)
        self.assertNotIn("agent_tool", engine.available_tools | engine.latent_tools)
        self.assertNotIn("missing_plugin", engine.available_tools | engine.latent_tools)

    async def test_invalid_checkpoint_does_not_change_the_live_catalog(self):
        engine = self.engine(); engine.activate_tools(["task_list_tool"])
        before = set(engine.available_tools)
        for value in ("task_list_tool", [False], [None], {"task_list_tool": True}):
            with self.assertRaises(ValueError): restore_tool_catalog(engine, {"activated_tools": value})
            self.assertEqual(set(engine.available_tools), before)
        self.assertEqual(engine.activated_tools, {"task_list_tool"})

    async def test_new_role_defaults_are_not_mistaken_for_previous_session_activation(self):
        engine = self.engine(); engine.activate_tools(["task_list_tool"])
        configure_role(engine, {}, allowed_tools=["task_list_tool"])
        restore_tool_catalog(engine, {})
        self.assertIn("task_list_tool", engine.available_tools)

    async def test_isolation_is_rechecked_when_restoring_visibility(self):
        engine = self.engine()
        with patch.object(type(engine.execution_sandbox), "enabled", new_callable=lambda: property(lambda _: True)):
            restore_tool_catalog(engine, {"activated_tools": ["browser_tool"]})
        self.assertNotIn("browser_tool", engine.available_tools)

    async def test_late_mcp_tools_restore_only_inside_the_current_role_scope(self):
        engine = self.engine()
        configure_role(engine, {"mcp_servers": ["lab"]}, allowed_tools=None)
        restore_tool_catalog(engine, {"activated_tools": ["mcp__lab__tasks", "mcp__other__tasks"]})
        self.assertNotIn("mcp__lab__tasks", engine.available_tools)
        template = engine.latent_tools["task_list_tool"]
        tools = {}
        for server in ("lab", "other"):
            tool = copy.copy(template); tool.name = "mcp__" + server + "__tasks"; tool.server_name = server
            tools[tool.name] = tool
        self.assertEqual(engine.register_mcp_tools(tools), {"mcp__lab__tasks"})
        self.assertIn("mcp__lab__tasks", engine.available_tools)
        self.assertNotIn("mcp__other__tasks", engine.available_tools | engine.latent_tools)
        self.assertFalse(self.records)

    async def test_reapplying_a_remote_role_keeps_discovery_visible(self):
        engine = self.engine(); engine.activate_tools(["task_list_tool"])
        configure_role(engine, {}, allowed_tools=None)
        self.assertEqual(engine.activated_tools, {"task_list_tool"})
        self.assertIn("task_list_tool", engine.available_tools)


class ProgramHelpTests(unittest.TestCase):
    def test_flags_must_be_complete_tokens_not_similar_prefixes(self):
        self.assertFalse(supports("--session-id <id> --session-dir <dir>", ["--session"]))
        self.assertFalse(supports("--no-tools", ["--tools"]))
        self.assertTrue(supports("-s, --session <path>\n--session-id <id>", ["--session", "--session-id"]))
        self.assertFalse(supports("--system-prompt-file <path>", ["--system-prompt"]))
