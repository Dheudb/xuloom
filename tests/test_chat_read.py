"""Link-addressed history is data, not navigation or task recovery."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from xuloom.core.storage import atomic_json, history_root
from xuloom.features.chat_links import ChatLinkError, chat_link, read_link, remote_chat_link


class ChatReadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.project = self.root/'other-project'; self.project.mkdir()
        self.env = patch.dict(os.environ, {'XULOOM_DATA_DIR': str(self.root/'data'), 'XULOOM_CONFIG_DIR': str(self.root/'config')})
        self.env.start(); self.addCleanup(self.env.stop)
        self.folder = history_root(self.project); self.sid = 'saved-chat'
        atomic_json(self.folder/'project.json', {'version': 1, 'project': str(self.project.resolve())})
        self.path = self.folder/'sessions'/f'{self.sid}.json'
        self.raw = {'messages': [{'role': 'system', 'content': 'PRIVATE_CONFIGURATION'},
            {'role': 'user', 'content': 'Earlier question'}, {'role': 'assistant', 'content': 'Saved answer',
             '_openai_reasoning': {'content': 'PRIVATE_PROTOCOL'}}], 'metadata': {'api_key': 'PRIVATE_KEY'}}
        atomic_json(self.path, self.raw); self.url = chat_link(self.project, self.sid)

    async def test_read_cross_project_is_paged_and_does_not_execute_or_modify_history(self):
        before = self.path.read_bytes(); stat = self.path.stat()
        with patch('xuloom.core.engine.QueryEngine', side_effect=AssertionError('No engine or recovery')):
            first = await read_link(self.url, limit=1)
            second = await read_link(self.url, limit=1, cursor=first['nextCursor'])
        self.assertEqual(first['entries'][0]['text'], 'Earlier question')
        self.assertEqual(second['entries'][0]['text'], 'Saved answer')
        self.assertTrue(first['readOnly']); self.assertIsNone(second['nextCursor'])
        self.assertEqual(self.path.read_bytes(), before); self.assertEqual(self.path.stat().st_mtime_ns, stat.st_mtime_ns)
        for private in ['PRIVATE_CONFIGURATION', 'PRIVATE_PROTOCOL', 'PRIVATE_KEY']:
            self.assertNotIn(private, json.dumps(first)+json.dumps(second))

    async def test_archive_and_missing_workspace_remain_readable_without_restore(self):
        atomic_json(self.folder/'index.json', {'version': 1, 'sessions': {self.sid: {'archived': True}}})
        self.project.rmdir()
        page = await read_link(self.url)
        self.assertTrue(page['archived']); self.assertFalse(page['workspaceExists'])
        self.assertFalse(self.project.exists()); self.assertEqual(len(page['entries']), 2)

    async def test_changed_or_cross_conversation_cursor_is_rejected(self):
        first = await read_link(self.url, limit=1)
        self.raw['messages'].append({'role': 'assistant', 'content': 'new answer'}); atomic_json(self.path, self.raw)
        with self.assertRaisesRegex(ValueError, 'changed'):
            await read_link(self.url, cursor=first['nextCursor'])
        with self.assertRaises(ValueError): await read_link(self.url, limit=0)

    async def test_invalid_missing_and_foreign_agent_links_do_not_create_history(self):
        for bad in [self.url+'?prompt=run', self.url+'#fragment', 'https://example.org', self.url+'\n']:
            with self.assertRaises(ChatLinkError): await read_link(bad)
        with self.assertRaises(ValueError): await read_link(self.url, agent_id='foreign-agent')
        self.path.unlink()
        with self.assertRaises(ChatLinkError) as caught: await read_link(self.url)
        self.assertEqual(caught.exception.code, 'not_found'); self.assertFalse(self.path.exists())

    async def test_ssh_reads_only_the_verified_target_without_initialize_or_prompt(self):
        from xuloom.remote.config import validate_profile, save
        profile = validate_profile({'id': 'lab', 'name': 'Lab', 'host': 'fixture-host', 'cwd': '/work'})
        save(profile); url = remote_chat_link(profile, self.sid)
        page = {'entries': [{'role': 'assistant', 'kind': 'text', 'text': 'Remote answer'}],
                'nextCursor': None, 'sessionId': self.sid, 'readOnly': True}
        bridge = AsyncMock(); bridge.dispatch.return_value = page
        with patch('xuloom.remote.proxy.SSHBridge', return_value=bridge) as factory:
            result = await read_link(url, limit=3)
        self.assertEqual(result['entries'], page['entries'])
        self.assertEqual(factory.call_args.args[:3], ('lab', profile, None))
        bridge.dispatch.assert_awaited_once_with('session.transcript', {'sessionId': self.sid, 'cursor': None, 'limit': 3, 'agentId': None})
        bridge.close.assert_awaited_once()

    async def test_remote_failure_is_closed_without_local_fallback(self):
        from xuloom.remote.config import save
        profile = save({'id': 'lab', 'name': 'Lab', 'host': 'fixture-host', 'cwd': '/work'})
        bridge = AsyncMock(); bridge.dispatch.side_effect = ConnectionError('offline')
        with patch('xuloom.remote.proxy.SSHBridge', return_value=bridge):
            with self.assertRaises(ConnectionError): await read_link(remote_chat_link(profile, self.sid))
        bridge.close.assert_awaited_once()

    async def test_formal_cli_reads_the_same_projection_without_model_configuration(self):
        env = {k:v for k,v in os.environ.items() if not k.startswith(('OPENAI_', 'ANTHROPIC_'))}
        env['PYTHONIOENCODING'] = 'utf-8'
        process = await asyncio.to_thread(subprocess.run, [sys.executable, '-I', '-c',
            "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('xuloom',run_name='__main__')",
            str(Path(__file__).resolve().parents[1]), 'transcript',
            '--link', self.url, '--limit', '1', '--json'], env=env, capture_output=True, text=True, encoding='utf-8', timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn('Earlier question', process.stdout); self.assertNotIn('Saved answer', process.stdout)
        self.assertNotIn('PRIVATE_', process.stdout)


    async def test_remote_child_history_does_not_initialize_or_resume_worker(self):
        from xuloom.features.agent_history import inspect
        owner = {'parent_session_id': self.sid, 'agent_id': 'child'}
        worker = {'executor': 'ssh:lab', 'dispatch': {'remote': {'profile': {}, 'open': {'owner': owner}}}}
        client = AsyncMock(); client.request.return_value = {'entries': [], 'readOnly': True}
        with patch('xuloom.features.agent_history.record', return_value=({}, worker)), \
             patch('xuloom.agents.executors.profile', return_value={}), \
             patch('xuloom.remote.worker_protocol.session_id', return_value='remote-child'), \
             patch('xuloom.remote.ssh.SSHClient', return_value=client):
            await inspect(self.project, self.sid, agent_id='child')
        client.request.assert_awaited_once_with('session.transcript', {'sessionId': 'remote-child',
            'cursor': None, 'limit': 40, 'imageIndex': None, 'version': None})
        client.aclose.assert_awaited_once()

    async def test_read_only_rpc_works_without_current_session_or_engine(self):
        from xuloom.desktop.bridge import DesktopBridge
        bridge = DesktopBridge(lambda _: None, cwd=self.project)
        self.assertIsNone(bridge.engine); self.assertIsNone(bridge.resume_id)
        result = await bridge.dispatch('session.transcript', {'sessionId': self.sid, 'limit': 1})
        self.assertEqual(result['entries'][0]['text'], 'Earlier question')
        self.assertIsNone(bridge.engine); self.assertIsNone(bridge.resume_id)
        bridge.resume_id = self.sid
        with self.assertRaises(ValueError):
            await bridge.dispatch('session.transcript', {'sessionId': ''})

    async def test_tool_errors_and_cancellation_are_not_disguised_as_success(self):
        from xuloom.tools.chat_read_tool import ChatReadTool
        tool = ChatReadTool()
        result = await tool.execute('xuloom://chat/invalid')
        self.assertFalse(result['ok']); self.assertTrue(result['is_error'])
        self.assertEqual(result['metadata']['code'], 'invalid_link')
        self.assertEqual(tool.prompt(), '')
        with patch('xuloom.tools.chat_read_tool.read_link', side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError): await tool.execute(self.url)


class ChatReadEngineTests(unittest.IsolatedAsyncioTestCase):
    from test_tool_recovery import RecoveryTests as _Fixture
    asyncSetUp = _Fixture.asyncSetUp
    asyncTearDown = _Fixture.asyncTearDown
    fixture = _Fixture.fixture

    async def test_explicit_link_activates_only_the_reader_and_target_remains_unchanged(self):
        target = self.root/'target'; target.mkdir(); folder = history_root(target)
        atomic_json(folder/'project.json', {'version': 1, 'project': str(target.resolve())})
        file = folder/'sessions'/'other.json'
        atomic_json(file, {'messages': [{'role': 'assistant', 'content': 'TARGET_EVIDENCE'}], 'metadata': {}})
        before = file.read_bytes(); url = chat_link(target, 'other')
        self.assertNotIn('chat_read_tool', {s.get('name') or s.get('function', {}).get('name') for s in self.engine._get_anthropic_tools_schema()})
        self.fixture([('chat_read_tool', json.dumps({'url': url})), 'Read and compared'])
        result = await self.engine.run('Read '+url, sys_print_callback=lambda _: None)
        self.assertTrue(result.ok, result.text)
        self.assertIn('chat_read_tool', {s['function']['name'] for s in self.requests[0]['tools']})
        self.assertIn('TARGET_EVIDENCE', json.dumps(self.requests[-1]['messages']))
        self.assertEqual(self.engine.session_cwd, str(self.root))
        self.assertEqual(file.read_bytes(), before)

    async def test_history_discovery_uses_existing_tool_search(self):
        result = await self.engine.available_tools['tool_search_tool'].execute('读取其他项目的会话历史')
        self.assertIn('chat_read_tool', result['content'])
        self.assertNotIn('chat_read_tool', self.engine.activated_tools)

    async def test_link_cannot_bypass_disabled_or_role_denied_tools(self):
        from dataclasses import replace
        original = self.engine.config
        self.engine.config = replace(original, disabled_tools={'chat_read_tool'})
        self.fixture(['Not permitted'])
        result = await self.engine.run('Read xuloom://chat/v1/0000000000000000/saved', sys_print_callback=lambda _: None)
        self.assertTrue(result.ok)
        names = {item['function']['name'] for item in self.requests[0]['tools']}
        self.assertNotIn('chat_read_tool', names)
        self.engine.config = original
        self.engine.tool_context['activate_tool_filter'] = lambda name, tool: name != 'chat_read_tool'
        self.assertEqual(self.engine.activate_tools(['chat_read_tool']), [])
