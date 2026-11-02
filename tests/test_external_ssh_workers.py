"""Installed external CLIs on the execution side of real encrypted OpenSSH.

All model requests terminate on owned loopback fixtures; no account or supplier
quota is used. Each case owns its controller, SSH server and remote history.
"""
import asyncio
import json
import os
import shutil
import threading
import unittest
from pathlib import Path

from tests import ssh_fixture
from tests.external_discovery_api import CatalogModel
from tests.test_codex_responses import ResponsesModel
from tests import test_native_ssh_workers as native
from xuloom.core.storage import IOPath, history_root
from xuloom.core.engine import QueryEngine
from xuloom import __version__


class ExternalSSHContracts:
    backend = None

    async def asyncSetUp(self):
        self.native = native.NativeSSHTests()
        await self.native.asyncSetUp()
        self.root = self.native.root
        self.host = self.native.fixture
        self.program = os.environ['XULOOM_TEST_' + self.backend.upper() + '_PROGRAM']
        self.native.role.write_text(self.native.role.read_text(encoding='utf-8').replace(
            'executor: ssh:test', 'executor: ssh:test\nbackend: ' + self.backend +
            '\nbackend_program: ' + self.program.replace('\\', '/') + '\ntools: [Read, Write]'), encoding='utf-8')
        config = self.root / 'remote-config/config.json'
        data = json.loads(config.read_text(encoding='utf-8'))
        connection = data['model_connections'][0]
        self.provider = 'anthropic' if self.backend == 'claude' else 'openai'
        connection.update(provider=self.provider, base_url=self.host.local.base + '/remote' + ('/v1' if self.provider == 'openai' else ''))
        config.write_text(json.dumps(data), encoding='utf-8')
        (self.host.remote / 'evidence.txt').write_text('REMOTE_EXECUTION_HOST_EVIDENCE', encoding='utf-8')
        (self.root / 'evidence.txt').write_text('LOCAL_MUST_NOT_BE_READ', encoding='utf-8')
        self.model = ResponsesModel(mode='namespace') if self.backend == 'codex' else CatalogModel(self.provider)
        self.writing = False
        self.write_path = 'denied.txt'
        self.hold_after_effect = False
        self.gate = None
        self.entered = threading.Event()
        self.host.local.response_handler = self.respond

    async def asyncTearDown(self):
        if self.gate: self.gate.set()
        await self.native.asyncTearDown()

    @staticmethod
    def tool_outputs(body):
        if 'input' in body:
            return [item['output'] for item in body['input'] if item.get('type') == 'function_call_output']
        return [message['content'] for message in body['messages'] if message.get('role') == 'tool' or
            isinstance(message.get('content'), list) and any(block.get('type') == 'tool_result' for block in message['content'])]

    def respond(self, handler, body):
        if not body.get('stream'):
            return CatalogModel.json(handler, {'input_tokens': 500})
        held = self.gate and (not self.hold_after_effect or self.tool_outputs(body))
        if not self.gate or held: self.entered.set()
        if held: self.gate.wait(25)
        try:
            if self.backend == 'codex':
                self.model.arguments = {'file_path': self.write_path, 'content': 'MUST_NOT_WRITE'} if self.writing else {'file_path': 'evidence.txt'}
                self.model.tool = 'file_write_tool' if self.writing else 'file_read_tool'
                return self.model(handler, body)
            final = bool(self.tool_outputs(body))
            return self.model.reply(handler, body, text='REMOTE_EXTERNAL_VERIFIED',
                name=None if final else 'mcp__xuloom__' + ('file_write_tool' if self.writing else 'file_read_tool'),
                values={'file_path': self.write_path, 'content': 'MUST_NOT_WRITE'} if self.writing else {'file_path': 'evidence.txt'}, identity='remote-owned-tool')
        except (OSError, BrokenPipeError):
            return True  # The owner deliberately closed this fixture connection.

    async def spawn(self, **values):
        result = await self.native.spawn(**values)
        return result['agents'][0]

    async def test_readonly_history_runs_on_execution_host_without_restarting_worker(self):
        from xuloom.features.agent_history import inspect
        row = await self.spawn(run_in_background=False)
        self.assertEqual(row['status'], 'completed', row)
        engine = self.native.engine
        versions = dict(engine.session_manager._versions)
        path = IOPath(engine.session_manager.get_subagent_transcript_path(row['agent_id']))
        before = path.read_bytes(), path.stat().st_mtime_ns
        requests = len(self.host.local.records)
        remote_before = self.remote_state(row)
        page = await inspect(self.root, engine.session_id, agent_id=row['agent_id'])
        self.assertEqual(page['backend'], self.backend)
        evidence = [entry for entry in page['entries'] if entry['kind'] == 'tool_result']
        self.assertTrue(any('REMOTE_EXECUTION_HOST_EVIDENCE' in entry['text'] for entry in evidence), page)
        self.assertNotIn('LOCAL_MUST_NOT_BE_READ', json.dumps(page))
        self.assertEqual(len(self.host.local.records), requests)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
        self.assertEqual(engine.session_manager._versions, versions)
        self.assertEqual(self.remote_state(row), remote_before)
        from xuloom.cli import parser
        from xuloom.remote.cli import dispatch
        remote_sid = self.native.tool._worker(row['agent_id'])['remote_reference']['session_id']
        args = parser().parse_args(['remote', 'transcript', 'test', '--session', remote_sid, '--json'])
        events = []
        async def emit(event): events.append(event)
        cli = await dispatch(args, emit)
        self.assertTrue(cli['ok'], cli)
        self.assertEqual(cli['transcript']['entries'], page['entries'])
        self.assertEqual(len(self.host.local.records), requests)

    def remote_state(self, row):
        reference = self.native.tool._worker(row['agent_id'])['remote_reference']
        path = self.root / 'remote-data/history' / history_root(self.host.remote).name / 'sessions' / (reference['session_id'] + '.json')
        return json.loads(IOPath(path).read_text(encoding='utf-8'))

    def assert_remote_requests(self):
        self.assertTrue(self.host.local.records)
        for record in self.host.local.records:
            self.assertTrue(record['path'].startswith('/remote/'), record['path'])
            self.assertEqual(record['body']['model'], 'opaque-model')
            headers = {key.lower(): value for key, value in record['headers'].items()}
            self.assertEqual(headers.get('authorization') or headers.get('x-api-key'),
                'Bearer key-remote' if 'authorization' in headers else 'key-remote')
            self.assertNotIn('parent-private-never-transfer', json.dumps(record))

    async def test_execution_host_evidence_and_native_identity_survive_followup(self):
        row = await self.spawn(run_in_background=False)
        self.assertEqual(row['status'], 'completed', row)
        state = self.remote_state(row)['metadata']['external_agent']
        self.assertEqual(state['backend'], self.backend)
        journal = state.get('session_file') or state.get('rollout_path')
        if journal: self.assertTrue(IOPath(journal).is_file())
        outputs = str([self.tool_outputs(record['body']) for record in self.host.local.records])
        self.assertIn('REMOTE_EXECUTION_HOST_EVIDENCE', outputs)
        self.assertNotIn('LOCAL_MUST_NOT_BE_READ', outputs)
        reference = self.native.tool._worker(row['agent_id'])['remote_reference']
        result = json.loads(await self.native.tool.execute(action='followup', agent_id=row['agent_id'],
            prompt='Explain the remote evidence', response_format='json'))
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(result['remote_reference']['session_id'], reference['session_id'])
        after = self.remote_state(row)['metadata']['external_agent']
        self.assertEqual(after['session_id'], state['session_id'])
        self.assertEqual(len(self.host.local.records), 3)
        self.assert_remote_requests()
        self.assertEqual(self.native.engine.tool_context['collaboration_run']['output_reserved'], 0)
        self.assertTrue(self.native.engine.tool_context['subagent_tasks'].backends[row['agent_id']].client.closed)

    async def test_parent_denial_reaches_external_mcp_before_a_remote_write(self):
        self.writing = True
        self.native.engine.permission_manager.always_deny_tools.add('file_write_tool')
        row = await self.spawn(run_in_background=False)
        self.assertEqual(row['status'], 'completed', row)
        self.assertFalse((self.host.remote / 'denied.txt').exists())
        self.assertIn('Delegating session', str([self.tool_outputs(record['body']) for record in self.host.local.records]))
        self.assert_remote_requests()

    async def test_new_controller_restores_frozen_dispatch_and_the_same_external_session(self):
        row = await self.spawn(run_in_background=False)
        self.assertEqual(row['status'], 'completed', row)
        before = self.remote_state(row)['metadata']['external_agent']
        original_session = self.native.engine.session_id
        await self.native.engine.aclose()
        self.native.role.write_text(self.native.role.read_text(encoding='utf-8').replace(
            self.program.replace('\\', '/'), (self.root / 'changed-program-must-not-run.exe').as_posix()), encoding='utf-8')
        self.native.engine = QueryEngine(model='replacement-parent', model_provider='openai',
            api_key='replacement-parent-never-transfer', api_base_url=self.host.local.base + '/parent/v1', cwd=self.root)
        self.assertTrue(self.native.engine.load_session(original_session))
        self.native.tool = self.native.engine.available_tools['agent_tool']
        result = json.loads(await self.native.tool.execute(action='followup', agent_id=row['agent_id'],
            prompt='Continue the original remote assignment', response_format='json'))
        self.assertEqual(result['status'], 'completed', result)
        after = self.remote_state(row)['metadata']['external_agent']
        self.assertEqual(after['session_id'], before['session_id'])
        self.assertEqual(after['binding'], before['binding'])
        self.assertEqual(len(self.host.local.records), 3)
        self.assert_remote_requests()
        self.assertNotIn('replacement-parent-never-transfer', str(self.host.local.records))

    async def test_receipt_changed_after_binding_is_preserved_before_another_model_request(self):
        row = await self.spawn(run_in_background=False)
        self.assertEqual(row['status'], 'completed', row)
        manager = self.native.engine.session_manager
        path = IOPath(manager.get_subagent_transcript_path(row['agent_id']))
        changed = json.loads(path.read_text(encoding='utf-8'))
        changed['result'] = 'CHANGED_AFTER_BINDING'
        preserved = json.dumps(changed).encode()
        path.write_bytes(preserved)
        requests, channels = len(self.host.local.records), len(self.host.server.commands)
        result = json.loads(await self.native.tool.execute(action='followup', agent_id=row['agent_id'],
            prompt='Do not run from an old receipt snapshot', response_format='json'))
        self.assertEqual(result['status'], 'crashed', result)
        self.assertEqual(path.read_bytes(), preserved)
        self.assertEqual(len(self.host.local.records), requests)
        self.assertEqual(len(self.host.server.commands), channels)

    @unittest.skipUnless(os.environ.get('XULOOM_TEST_CONTROLLER_PYTHON'), 'Set XULOOM_TEST_CONTROLLER_PYTHON for fresh installed controller processes')
    async def test_two_installed_controller_processes_keep_the_owned_remote_session(self):
        executable = os.environ['XULOOM_TEST_CONTROLLER_PYTHON']
        config = self.root / 'controller-config'; config.mkdir()
        settings = json.loads(self.native.config_file.read_text(encoding='utf-8'))
        settings['ssh_connections'][0]['python'] = executable
        (config / 'config.json').write_text(json.dumps(settings), encoding='utf-8')
        environment = {**os.environ, 'XULOOM_CONFIG_DIR': str(config)}
        helper = Path(__file__).with_name('external_ssh_controller_fixture.py')
        async def controller(*args):
            process = await asyncio.create_subprocess_exec(executable, '-I', '-B', str(helper), __version__, str(self.root), *args,
                cwd=self.root, env=environment, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                **({'creationflags': 0x08000000} if os.name == 'nt' else {}))
            try:
                output, errors = await asyncio.wait_for(process.communicate(), 60)
            finally:
                if process.returncode is None: process.kill(); await process.wait()  # This fixture's child only.
            self.assertEqual(process.returncode, 0, errors.decode('utf-8', 'replace'))
            lines = [line.removeprefix('XULOOM_CONTROLLER_RESULT=') for line in output.decode('utf-8').splitlines()
                if line.startswith('XULOOM_CONTROLLER_RESULT=')]
            self.assertEqual(len(lines), 1)
            return json.loads(lines[0])
        first = await controller('spawn')
        self.native.role.write_text(self.native.role.read_text(encoding='utf-8').replace(
            self.program.replace('\\', '/'), (self.root / 'changed-program-must-not-run.exe').as_posix()), encoding='utf-8')
        second = await controller('followup', first['session_id'], first['agent_id'])
        self.assertEqual(second['remote_reference']['session_id'], first['remote_reference']['session_id'])
        self.assertGreater(second['revision'], first['revision'])
        self.assertEqual(len(self.host.local.records), 3)
        self.assert_remote_requests()
        self.assertNotIn('controller-key-never-transfer', str(self.host.local.records))

    async def test_missing_execution_host_program_never_requests_a_model_or_falls_back(self):
        text = self.native.role.read_text(encoding='utf-8')
        self.native.role.write_text(text.replace(self.program.replace('\\', '/'), (self.root / 'absent-backend.exe').as_posix()), encoding='utf-8')
        row = await self.spawn(run_in_background=False)
        self.assertNotEqual(row['status'], 'completed', row)
        self.assertEqual(self.host.local.records, [])
        self.assertEqual(self.native.engine.tool_context['subagent_tasks'].running, set())

    async def test_owner_cancel_releases_remote_process_and_does_not_automatically_replay(self):
        self.gate = threading.Event()
        row = await self.spawn(run_in_background=True)
        identity = row['agent_id']
        self.assertTrue(await asyncio.to_thread(self.entered.wait, 20))
        cancelled = json.loads(await asyncio.wait_for(self.native.tool.execute(action='cancel', agent_id=identity), 45))
        self.assertEqual(cancelled['status'], 'cancelled', cancelled)
        self.gate.set()
        self.assertEqual(self.native.engine.tool_context['subagent_tasks'].running, set())
        self.assertEqual(len(self.host.local.records), 1)
        self.assertEqual(self.native.engine.tool_context['collaboration_run']['output_reserved'], 0)
        self.assertNotEqual(self.remote_state(row)['metadata']['external_agent']['phase'], 'running')

    async def test_transport_loss_after_file_effect_is_not_replayed_or_completed_before_the_model(self):
        self.writing, self.hold_after_effect = True, True
        self.write_path = 'one-effect.txt'
        self.gate = threading.Event()
        row = await self.spawn(run_in_background=True)
        identity = row['agent_id']
        self.assertTrue(await asyncio.to_thread(self.entered.wait, 20))
        path = self.host.remote / self.write_path
        self.assertEqual(path.read_text(encoding='utf-8'), 'MUST_NOT_WRITE')
        before = path.stat().st_mtime_ns
        self.host.server.disconnect()
        await asyncio.wait_for(self.native.engine.tool_context['subagent_tasks'].tasks[identity], 45)
        self.assertNotEqual(self.native.tool._worker(identity)['status'], 'completed')
        result = json.loads(await self.native.tool.execute(action='result', agent_id=identity, response_format='json'))
        self.assertNotEqual(result['status'], 'completed', result)
        self.assertEqual(len(self.host.local.records), 2)
        self.assertEqual(path.stat().st_mtime_ns, before)
        self.assertEqual(self.native.engine.tool_context['collaboration_run']['output_reserved'], 0)
        self.gate.set()


def enabled(backend):
    return bool(ssh_fixture.paramiko is not None and shutil.which('ssh') and os.environ.get('XULOOM_TEST_' + backend.upper() + '_PROGRAM'))


@unittest.skipUnless(enabled('claude'), 'Installed Claude, system OpenSSH and development SSH fixture required')
class ClaudeSSHTests(ExternalSSHContracts, unittest.IsolatedAsyncioTestCase):
    backend = 'claude'


@unittest.skipUnless(enabled('pi'), 'Installed Pi, system OpenSSH and development SSH fixture required')
class PiSSHTests(ExternalSSHContracts, unittest.IsolatedAsyncioTestCase):
    backend = 'pi'


@unittest.skipUnless(enabled('codex'), 'Installed Codex, system OpenSSH and development SSH fixture required')
class CodexSSHTests(ExternalSSHContracts, unittest.IsolatedAsyncioTestCase):
    backend = 'codex'


@unittest.skipUnless(enabled('dsh'), 'Installed DSH, system OpenSSH and development SSH fixture required')
class DshSSHTests(ExternalSSHContracts, unittest.IsolatedAsyncioTestCase):
    backend = 'dsh'
