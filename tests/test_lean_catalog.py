"""Small first requests retain discovery, role filtering and full validation."""
import copy
import json
import os
import unittest
from types import SimpleNamespace

import test_agent_lifecycle as lifecycle
from xuloom.core.tool_catalog import compact_definition


class LeanCatalogTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = lifecycle.LifecycleTests.asyncSetUp
    asyncTearDown = lifecycle.LifecycleTests.asyncTearDown
    make_engine = lifecycle.LifecycleTests.make_engine

    def names(self):
        return {item.get('function', item)['name'] for item in self.engine._get_anthropic_tools_schema()}

    async def test_default_catalog_is_small_and_activation_survives_restore(self):
        self.assertEqual(self.names(), {'command_tool', 'file_read_tool', 'file_edit_tool', 'file_write_tool', 'tool_search_tool'})
        self.assertIn('agent_tool', self.engine.available_tools)
        expected_shell = 'cmd.exe' if os.name == 'nt' else '/bin/sh'
        self.assertIn('shell: ' + expected_shell, self.engine.context_builder.generate_system_prompt())
        self.assertNotIn('Function Result Clearing', self.engine._build_tool_prompt_summary())
        schemas = json.dumps(self.engine._get_anthropic_tools_schema(), ensure_ascii=False, separators=(',', ':'))
        self.assertLess(len(schemas), 3900)
        result = await self.engine.available_tools['tool_search_tool'].execute('web search', activate=['web_search_tool'])
        self.assertIn('web_search_tool', result['metadata']['activated'])
        self.assertIn('web_search_tool', self.names())
        self.assertTrue(self.engine.persist_session_state())
        self.engine.session_manager.release_writer()
        restored = self.make_engine()
        self.assertTrue(restored.load_session(self.engine.session_id))
        self.assertIn('web_search_tool', {row.get('function', row)['name'] for row in restored._get_anthropic_tools_schema()})

    async def test_ultra_plan_and_custom_tools_remain_visible(self):
        self.engine.set_collaboration_mode('ultra', persist=False)
        self.assertEqual(self.names(),{'ultra_answer','ultra_prepare'})
        self.engine.tool_context['collaboration_run']['assessment']={'kind':'work'}
        self.assertIn('spawn_agent', self.names())
        self.assertTrue({'web_search_tool', 'web_fetch_tool'} <= self.names())
        self.engine.set_mode('plan')
        self.assertNotIn('bash_tool', self.names())
        self.assertIn('exit_plan_mode', self.names())
        self.engine.set_mode('normal')
        from test_tool_recovery import RecordTool
        self.engine.available_tools['record_tool'] = RecordTool()
        self.assertIn('record_tool', self.names())
        self.engine.tool_context['activate_tool_filter'] = lambda name, tool: tool is not None and name != 'web_search_tool'
        self.assertEqual(self.engine.activate_tools(['web_search_tool']), [])
        self.assertEqual(self.engine.activate_tools(['web_fetch_tool']), ['web_fetch_tool'])

    async def test_explicit_role_tool_list_needs_no_discovery_tool(self):
        from xuloom.agents.role_runtime import configure_role
        configure_role(self.engine, {'system_prompt': 'Find evidence'}, allowed_tools=['grep_tool'])
        self.assertEqual(self.names(), {'grep_tool'})
        self.assertNotIn('tool_search_tool', self.engine.available_tools)

    async def test_ultra_search_visibility_cannot_restore_role_denied_tools(self):
        from xuloom.agents.role_runtime import configure_role
        self.engine.set_collaboration_mode('ultra', persist=False)
        configure_role(self.engine, {'web_search': 'disabled'}, allowed_tools=None)
        self.assertNotIn('web_search_tool', self.names())
        self.assertNotIn('web_fetch_tool', self.names())
        self.assertNotIn('spawn_agent', self.names())

    async def test_search_reuses_the_actual_sdk_default_without_guessing_another_host(self):
        self.engine.api_base_url = None
        self.engine.client.base_url = 'https://fixture.invalid/v1/'
        self.assertEqual(self.engine.tool_context['native_search_options']()['base_url'], 'https://fixture.invalid/v1/')

    def test_compaction_preserves_named_properties_refs_and_bounds(self):
        schema = {'name': 'custom', 'description': 'x' * 500, 'input_schema': {'type': 'object', 'title': 'Metadata',
            'default': {'title': 'Literal default', 'examples': [1]},
            'required': ['title', 'examples'], 'properties': {'title': {'type': 'string', 'maxLength': 12},
            'examples': {'$ref': '#/$defs/Choice'}}, '$defs': {'Choice': {'type': 'integer', 'minimum': 1, 'maximum': 50}}}}
        before = copy.deepcopy(schema)
        projected = compact_definition(schema)
        self.assertEqual(schema, before)
        self.assertNotIn('title', projected['input_schema'])
        self.assertEqual(projected['input_schema']['properties'], before['input_schema']['properties'])
        self.assertEqual(projected['input_schema']['$defs'], before['input_schema']['$defs'])
        self.assertEqual(projected['input_schema']['required'], ['title', 'examples'])
        self.assertEqual(projected['input_schema']['default'], before['input_schema']['default'])
