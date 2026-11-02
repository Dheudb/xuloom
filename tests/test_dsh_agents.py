"""Opt-in installed DSH, native worker records and owned localhost model wires."""
import json
import os
import unittest
import asyncio
import base64
import io
import tempfile
from pathlib import Path
from PIL import Image
from unittest.mock import patch, AsyncMock
from xuloom.core.storage import IOPath

if __package__:
    from . import test_external_agents as external
else:
    import test_external_agents as external

PROGRAM = os.environ.get('XULOOM_TEST_DSH_PROGRAM')


@unittest.skipUnless(external.MCP and PROGRAM, 'set XULOOM_TEST_DSH_PROGRAM for installed DSH / localhost checks')
class InstalledDshTests(external.ExternalFixture):
    @unittest.skipUnless(os.name == 'nt', 'Windows process current-directory limit')
    async def test_overlong_process_directory_fails_before_launch_or_model_request(self):
        from xuloom.agents.external.runtime import configure, ExternalExecutionError
        from xuloom.agents.external.dsh import sample
        with patch.dict(os.environ, {'XULOOM_DATA_DIR': str(self.root / ('d' * 90) / ('e' * 90))}):
            engine = self.engine('openai')
            configure(engine, {'backend': 'dsh', 'backend_program': str(PROGRAM)})
            engine.messages = [{'role': 'user', 'content': 'Read evidence.txt'}]
            with self.assertRaises(ExternalExecutionError) as raised:
                await sample(engine, system_prompt='Read evidence.', api_messages=engine.messages,
                    turn_count=1, model_name=engine.primary_model, max_tokens=1024)
            self.assertEqual(raised.exception.reason, 'external_directory_unsupported')
            self.assertFalse(self.records)
            self.assertEqual(engine.tool_context['external_agent']['phase'], 'idle')

    def role(self, tools='Read'):
        folder = self.root / '.xuloom/agents'; folder.mkdir(parents=True, exist_ok=True)
        (folder / 'external-reader.md').write_text('---\nname: external-reader\ndescription: independent evidence\nbackend: dsh\nbackend_program: ' + str(PROGRAM).replace('\\', '/') + '\ntools: [' + tools + ']\n---\nUse the assigned native tool. Literal template: {{not_a_dsh_variable}}\n', encoding='utf-8')

    async def spawn(self, engine, **values):
        result = await engine.agents('spawn', prompt='Read evidence.txt', subagent_type='external-reader', **values)
        self.assertTrue(result['ok'], result)
        return result['data']['agents'][0]

    async def test_owned_openai_worker_and_fresh_process_followup_keep_identity_and_receipts(self):
        self.role(); engine = self.engine('openai')
        row = await self.spawn(engine)
        self.assertEqual(row['status'], 'completed', row)
        before = engine.session_manager.load_subagent_transcript(row['agent_id'])
        state = before['metadata']['external_agent']
        self.assertEqual(state['backend'], 'dsh')
        self.assertTrue(IOPath(state['session_file']).is_file())
        result = await engine.agents('followup', agent_id=row['agent_id'], prompt='Explain the evidence again')
        self.assertEqual(result['data']['status'], 'completed', result)
        after = engine.session_manager.load_subagent_transcript(row['agent_id'])['metadata']['external_agent']
        self.assertEqual(after['dsh_session_id'], state['dsh_session_id'])
        self.assertEqual((state['dsh_turn'], after['dsh_turn']), (1, 2))
        self.assertEqual(len(self.records), 3)
        self.assertEqual(engine.tool_context['collaboration_run']['output_used'], 15)
        self.assertTrue(all(record['headers']['authorization'] == 'Bearer fixture-only' for record in self.records))
        self.assertIn('OWNED_EVIDENCE', json.dumps(self.records[-1]['body']))

    async def test_anthropic_uses_the_selected_native_connection(self):
        self.role(); engine = self.engine()
        row = await self.spawn(engine)
        self.assertEqual(row['status'], 'completed', row)
        self.assertEqual(len(self.records), 2)
        self.assertTrue(all(record['headers']['x-api-key'] == 'fixture-only' for record in self.records))

    async def test_failed_native_tool_remains_a_failed_tool_not_a_fake_success(self):
        self.role(); engine = self.engine('openai'); (self.root / 'evidence.txt').unlink()
        row = await self.spawn(engine)
        self.assertEqual(row['status'], 'completed', row)
        transcript = engine.session_manager.load_subagent_transcript(row['agent_id'])
        activity = transcript['metadata']['tool_activity']['recent']
        self.assertEqual(len(activity), 1)
        self.assertTrue(activity[0]['tool_result']['is_error'])
        self.assertIn('does not exist', json.dumps(self.records[-1]['body']))

    async def test_unknown_native_builtin_is_not_executed(self):
        self.role(); self.mode = 'builtin'; engine = self.engine('openai')
        row = await self.spawn(engine)
        self.assertEqual(row['status'], 'crashed', row)
        self.assertEqual(row['receipt']['reason'], 'provider_error')
        self.assertEqual(len(self.records), 1)
        state = engine.session_manager.load_subagent_transcript(row['agent_id'])
        self.assertFalse(state['metadata']['tool_activity']['recent'])

    async def test_incomplete_provider_stream_does_not_release_a_tool(self):
        self.role(); self.mode = 'truncated'; engine = self.engine('openai')
        row = await self.spawn(engine)
        self.assertEqual(row['status'], 'crashed', row)
        self.assertEqual(len(self.records), 1)
        state = engine.session_manager.load_subagent_transcript(row['agent_id'])
        self.assertFalse(state['metadata']['tool_activity']['recent'])

    async def test_native_tool_image_is_durable_and_reaches_the_selected_model(self):
        self.role(); self.mode = 'image'; engine = self.engine('openai')
        (self.root / 'evidence.png').write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j5i8AAAAASUVORK5CYII='))
        row = await self.spawn(engine)
        self.assertEqual(row['status'], 'completed', row)
        self.assertEqual(len(self.records), 2)
        self.assertIn('data:image/png;base64,', json.dumps(self.records[-1]['body']))

    async def test_unknown_interruption_is_not_automatically_replayed(self):
        self.role(); self.mode = 'hold'; engine = self.engine('openai')
        row = await self.spawn(engine, run_in_background=True)
        async def entered():
            while not self.records: await asyncio.sleep(.02)
        await asyncio.wait_for(entered(), 20)
        result = await engine.agents('cancel', agent_id=row['agent_id'])
        self.assertTrue(result['ok'], result)
        self.release.set()
        before = len(self.records)
        result = await engine.agents('followup', agent_id=row['agent_id'], prompt='continue')
        self.assertEqual(result['data']['receipt']['reason'], 'external_outcome_unknown', result)
        self.assertEqual(len(self.records), before)

    async def test_changed_history_is_refused_before_another_model_request(self):
        self.role(); engine = self.engine('openai')
        row = await self.spawn(engine)
        self.assertEqual(row['status'], 'completed', row)
        state = engine.session_manager.load_subagent_transcript(row['agent_id'])['metadata']['external_agent']
        with IOPath(state['session_file']).open('ab') as stream: stream.write(b'\n')
        before = len(self.records)
        result = await engine.agents('followup', agent_id=row['agent_id'], prompt='continue')
        self.assertEqual(result['data']['status'], 'crashed', result)
        self.assertEqual(result['data']['receipt']['reason'], 'external_history_changed')
        self.assertEqual(len(self.records), before)

    async def test_project_environment_and_personal_backend_configuration_are_not_loaded(self):
        self.role(); engine = self.engine('openai')
        (self.root / '.env').write_text('DSH_UNOWNED_ENV=PRIVATE_MARKER\nXULOOM_DSH_TOKEN=wrong-project-key\n')
        (self.root / '.dsh').mkdir(); (self.root / '.dsh/cordis.patch.yml').write_text('not valid configuration')
        poison = self.root / 'personal-startup.js'; poison.write_text('throw new Error("PERSONAL_STARTUP_MUST_NOT_RUN")')
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'never-forward-personal-key', 'DSH_HOME': str(self.root / '.dsh'),
                'NODE_OPTIONS': '--require=' + str(poison), 'NODE_PATH': str(self.root / '.dsh')}):
            row = await self.spawn(engine)
        self.assertEqual(row['status'], 'completed', row)
        self.assertTrue(all(record['headers']['authorization'] == 'Bearer fixture-only' for record in self.records))
        self.assertNotIn('PRIVATE_MARKER', json.dumps(self.records))

    async def test_parent_denial_still_controls_the_installed_backend(self):
        self.role(); engine = self.engine(); engine.permission_manager.always_deny_tools.add('file_read_tool')
        row = await self.spawn(engine)
        self.assertEqual(row['status'], 'completed', row)
        self.assertNotIn('OWNED_EVIDENCE', json.dumps(self.records))
        state = engine.session_manager.load_subagent_transcript(row['agent_id'])
        self.assertTrue(state['metadata']['tool_activity']['recent'][0]['tool_result']['is_error'])

    async def test_large_final_answer_is_verified_from_the_complete_journal(self):
        self.role(); engine = self.engine('openai')
        if __package__:
            from .external_discovery_api import CatalogModel
        else:
            from external_discovery_api import CatalogModel
        long_text = '完整回答 🌌\n' * 5000
        model = CatalogModel('openai')
        def respond(handler, body):
            if any(row.get('role') == 'tool' for row in body['messages']):
                return model.reply(handler, body, text=long_text)
            return False
        self.parent_handler = respond
        row = await self.spawn(engine)
        self.assertEqual(row['status'], 'completed', row)
        self.assertEqual(row['result'], long_text)

    async def test_final_history_failure_never_reports_completed(self):
        self.role(); engine = self.engine('openai')
        from xuloom.agents.external.dsh_history import verify_history
        def corrupt(home, journal, state, host, final):
            text = journal.read_text(encoding='utf-8')
            journal.write_text(text.replace('OWNED_EVIDENCE', 'CORRUPTED_EVIDENCE'), encoding='utf-8')
            return verify_history(home, journal, state, host, final)
        with patch('xuloom.agents.external.dsh.verify_history', side_effect=corrupt):
            row = await self.spawn(engine)
        self.assertEqual(row['status'], 'crashed', row)
        self.assertEqual(row['receipt']['reason'], 'persistence_error')
        self.assertFalse(row['external_recovery']['resumable'])

    async def test_media_task_input_is_rejected_without_a_model_call_or_silent_drop(self):
        from xuloom.agents.external.runtime import configure, ExternalExecutionError
        from xuloom.agents.external.dsh import sample
        engine = self.engine('openai')
        configure(engine, {'backend': 'dsh', 'backend_program': str(PROGRAM)})
        data = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j5i8AAAAASUVORK5CYII='
        engine.messages = [{'role': 'user', 'content': [{'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': data}}]}]
        wire = [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + data}}]}]
        with self.assertRaises(ExternalExecutionError) as raised:
            await sample(engine, system_prompt='Inspect the input.', api_messages=wire, turn_count=1, model_name=engine.primary_model, max_tokens=1024)
        self.assertEqual(raised.exception.reason, 'external_input_unsupported')
        self.assertFalse(self.records)
        self.assertEqual(engine.tool_context['external_agent']['phase'], 'idle')


class DshBoundaryTests(unittest.TestCase):
    def test_shared_external_cleanup_removes_real_long_path_connection_files(self):
        import shutil
        from types import SimpleNamespace
        from xuloom.agents.external.runtime import cleanup
        from xuloom.core.storage import atomic_text
        with tempfile.TemporaryDirectory(prefix='xuloom-external-long-cleanup-') as directory:
            root = Path(directory)
            file = root / ('a' * 90) / ('b' * 90) / ('c' * 60) / 'connection.json'
            try:
                atomic_text(file, 'owned-invalidated-token')
                self.assertGreater(len(str(file)), 260); self.assertTrue(IOPath(file).is_file())
                asyncio.run(cleanup(SimpleNamespace(), {'phase': 'idle'}, None, [], [file]))
                self.assertFalse(IOPath(file).exists())
            finally:
                self.assertEqual(root.resolve().parent, Path(tempfile.gettempdir()).resolve())
                shutil.rmtree(IOPath(root))

    def test_shared_external_prepare_supports_a_deep_owned_history_home(self):
        import shutil
        from types import SimpleNamespace
        from xuloom.agents.external.runtime import prepare
        with tempfile.TemporaryDirectory(prefix='xuloom-external-long-prepare-') as directory:
            root = Path(directory); artifacts = root / ('a' * 90) / ('b' * 90)
            engine = SimpleNamespace(session_cwd=str(root), session_id='owner', model_provider='openai',
                messages=[{'role': 'user', 'content': 'task'}], tool_context={'external_backend': {'backend': 'pi'}},
                _input_contract=lambda _: None)
            try:
                with patch('xuloom.agents.external.runtime.verify', new=AsyncMock()), patch('xuloom.agents.external.runtime.artifact_directory', return_value=artifacts):
                    value = asyncio.run(prepare(engine, system_prompt='literal instructions', api_messages=engine.messages,
                        model_name='opaque-model', input_types={'text'}))
                self.assertGreater(len(str(value['home'])), 260)
                self.assertEqual(IOPath(value['home'] / 'instructions.txt').read_text(), 'literal instructions')
            finally:
                self.assertEqual(root.resolve().parent, Path(tempfile.gettempdir()).resolve())
                shutil.rmtree(IOPath(root))

    def test_image_projection_preserves_pixels_and_full_resolution(self):
        from xuloom.agents.external.dsh_history import project_result
        image = Image.new('LA', (800, 600), (113, 255)); file = io.BytesIO(); image.save(file, format='PNG')
        projected, digest = project_result('read', [{'type': 'image', 'mimeType': 'image/png', 'data': base64.b64encode(file.getvalue()).decode()}], False)
        data = base64.b64decode(projected[0]['data'])
        with Image.open(io.BytesIO(data)) as actual:
            self.assertEqual(actual.size, image.size)
            self.assertEqual(actual.getpixel((799, 599)), (113, 113, 113, 255))
        self.assertEqual(len(digest), 64)

    def test_coalesced_error_receipt_and_empty_result_have_explicit_projections(self):
        from xuloom.agents.external.dsh_history import result_digest
        expected = result_digest('read', [{'type': 'text', 'text': 'first'}, {'type': 'text', 'text': 'second'}], True)
        with tempfile.TemporaryDirectory() as directory:
            actual = result_digest('read', [{'type': 'text', 'text': 'Error: first\nsecond'}], True, home=Path(directory))
            self.assertEqual(actual, expected)
            self.assertEqual(result_digest('read', [], False), result_digest('read', [{'type': 'text', 'text': '(read returned no model-visible content)'}], False, home=Path(directory)))

    def test_an_external_saved_path_is_refused_before_it_is_read(self):
        from xuloom.agents.external.dsh_history import history_path
        from xuloom.agents.external.runtime import ExternalExecutionError
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); home = root / 'owned'; (home / 'sessions').mkdir(parents=True)
            outside = root / 'outside/session-00000000-0000-0000-0000-000000000001/session.v4.jsonl'
            outside.parent.mkdir(parents=True); outside.write_text('PRIVATE_SENTINEL')
            with self.assertRaises(ExternalExecutionError):
                history_path(home, outside.parent.name, str(outside))
            self.assertEqual(outside.read_text(), 'PRIVATE_SENTINEL')

    def test_junction_or_symlink_cannot_redefine_the_owned_history_root(self):
        import subprocess
        from xuloom.agents.external.dsh_history import history_path
        from xuloom.agents.external.runtime import ExternalExecutionError
        with tempfile.TemporaryDirectory(prefix='dsh-owned-boundary-') as directory:
            root = Path(directory); home = root / 'owned'; home.mkdir(); outside = root / 'outside'; outside.mkdir()
            link = home / 'sessions'
            if os.name == 'nt':
                result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)], capture_output=True)
                if result.returncode: self.skipTest('Owned directory junction is unavailable')
            else: link.symlink_to(outside, target_is_directory=True)
            try:
                with self.assertRaises(ExternalExecutionError): history_path(home, 'session-00000000-0000-0000-0000-000000000001')
            finally:
                if os.name == 'nt': link.rmdir()
                else: link.unlink()
