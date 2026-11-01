import unittest
from types import SimpleNamespace

from xuloom.agents.role_runtime import configure_role


class RoleRuntimeTests(unittest.TestCase):
    def engine(self):
        tool = lambda readonly=True, **values: SimpleNamespace(is_read_only=readonly, context={}, **values)
        return SimpleNamespace(available_tools={"file_read_tool": tool(), "file_write_tool": tool(False), "spawn_agent": tool(False)},
            latent_tools={"browser_tool": tool(), "mcp__lab__read": tool(server_name="lab"), "mcp__other__read": tool(server_name="other")},
            tool_context={"skill_registry": SimpleNamespace(read=lambda name: "Instructions: " + name)})

    def test_explicit_empty_parent_toolset_cannot_gain_host_tools(self):
        engine = self.engine()
        configure_role(engine, {}, allowed_tools=["*"], parent_tools={}, latent_tools={})
        self.assertEqual(engine.available_tools, {}); self.assertEqual(engine.latent_tools, {})

    def test_host_role_filters_tools_web_and_mcp_and_keeps_instance_context(self):
        engine = self.engine()
        configure_role(engine, {"read_only": True, "web_search": "disabled", "mcp_servers": ["lab"]}, allowed_tools=["*"])
        self.assertEqual(set(engine.available_tools), {"file_read_tool"})
        self.assertEqual(set(engine.latent_tools), {"mcp__lab__read"})
        self.assertTrue(all(tool.context is engine.tool_context for tool in [*engine.available_tools.values(), *engine.latent_tools.values()]))

    def test_team_channel_never_reintroduces_explicitly_denied_or_mutating_tools(self):
        for definition in ({"read_only": True}, {"disallowed_tools": ["send_message_tool"]}):
            engine = self.engine()
            configure_role(engine, definition, allowed_tools=["*"], team_tool=SimpleNamespace(is_read_only=False))
            self.assertNotIn("send_message_tool", engine.available_tools)

    def test_frozen_resume_does_not_reread_changed_or_deleted_skill(self):
        engine = self.engine()
        def missing(name): raise FileNotFoundError("The Skill changed after the worker started")
        engine.tool_context["skill_registry"].read = missing
        configure_role(engine, {"skills": ["old-skill"]}, allowed_tools=["file_read_tool"], frozen_prompt="ORIGINAL_FROZEN_INSTRUCTIONS")
        self.assertEqual(engine.tool_context["agent_system_prompt"], "ORIGINAL_FROZEN_INSTRUCTIONS")
        self.assertEqual(set(engine.available_tools), {"file_read_tool"})

    def test_new_role_loads_skills_and_reference_context_without_expanding_tool_access(self):
        engine = self.engine()
        configure_role(engine, {"system_prompt": "Assigned role", "skills": ["fixture"]}, allowed_tools=[], context_messages=[{"role": "user", "content": "Reference only"}])
        self.assertEqual(engine.available_tools, {}); self.assertEqual(engine.latent_tools, {})
        self.assertIn("Instructions: fixture", engine.tool_context["agent_system_prompt"])
        self.assertIn("Reference only", engine.tool_context["agent_system_prompt"])
