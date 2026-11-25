"""Saved CLI model selection uses the shared catalog, without model requests."""
import copy
import asyncio
from contextlib import nullcontext
from io import StringIO
import os
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch
import tempfile
from pathlib import Path
import subprocess
import sys
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from rich.console import Console
from xuloom.core.model_catalog import select_model
from xuloom.main import _interactive_model_switch


class CliModelPickerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cfg = {'active_model_connection': 'a', 'model_connections': [
            {'id': 'a', 'name': 'API A', 'provider': 'openai', 'model': 'shared',
             'models': ['shared', 'first'], 'base_url': 'https://gateway.invalid/v1', 'api_key': 'key-a', 'network_mode': 'auto'},
            {'id': 'b', 'name': 'API [B]', 'provider': 'openai', 'model': 'shared',
             'models': ['shared', 'gemini-model'], 'base_url': 'https://gateway.invalid/v1', 'api_key': 'key-b', 'network_mode': 'direct',
             'model_reasoning': {'gemini-model': {'adapter': 'openai', 'efforts': ['low', 'high'], 'effort': 'high'}}}]}
        self.cfg = select_model(self.cfg, 'a', 'shared')
        self.output = StringIO()
        self.console = Console(file=self.output, force_terminal=False)
        self.console.input = Mock(side_effect=AssertionError('Saved models must not require typed credentials'))
        self.tui = NS(choose_menu=Mock(), suspend_streaming=nullcontext)
        self.engine = NS(primary_model='shared', model_provider='openai', api_base_url='https://gateway.invalid/v1',
                         config=NS(model_connection_id='a', replace=Mock(return_value=NS(model_connection_id='b'))),
                         tool_context={'subagent_tasks': NS(tasks={})}, has_active_turn=lambda: False,
                         client=NS(close=AsyncMock()), compactor=NS(client=NS(close=AsyncMock())),
                         switch_model_runtime=Mock(), _sync_model_runtime_context=Mock(), persist_session_state=Mock(return_value=True),
                         model_selection={'connectionId': 'a', 'model': 'shared'})
        self.updates = []
        def update(change):
            self.cfg = change(copy.deepcopy(self.cfg))
            self.updates.append(copy.deepcopy(self.cfg))
            return self.cfg
        for target, value in [('xuloom.main.console', self.console),
                              ('xuloom.main.load_config', lambda: copy.deepcopy(self.cfg)),
                              ('xuloom.core.config.update_config', update)]:
            manager = patch(target, value);manager.start();self.addCleanup(manager.stop)
        manager = patch.dict(os.environ, {});manager.start();self.addCleanup(manager.stop)

    async def test_default_model_dialog_lists_all_saved_models_and_reuses_own_connection(self):
        self.tui.choose_menu.side_effect = ['saved:b', 'gemini-model']
        old_client, old_compactor = self.engine.client, self.engine.compactor.client
        await _interactive_model_switch(self.engine, self.tui)
        menus = self.tui.choose_menu.call_args_list
        self.assertIn('saved:a', [o['value'] for o in menus[0].kwargs['options']])
        self.assertIn('saved:b', [o['value'] for o in menus[0].kwargs['options']])
        self.assertEqual([o['value'] for o in menus[1].kwargs['options'] if o['value']], ['shared', 'gemini-model'])
        self.engine.switch_model_runtime.assert_called_once_with(provider='openai', model='gemini-model',
            api_base_url='https://gateway.invalid/v1', api_key='key-b', network_mode='direct', persist=False)
        self.assertEqual(self.engine.model_selection['connectionId'], 'b')
        self.assertEqual(self.engine.model_selection['model'], 'gemini-model')
        self.assertEqual(self.engine.reasoning_profile['effort'], 'high')
        self.assertEqual(self.cfg['active_model_connection'], 'b')
        self.assertEqual(self.cfg['model_connections'][0]['models'], ['shared', 'first'])
        old_client.close.assert_awaited_once(); old_compactor.close.assert_awaited_once()
        self.engine.persist_session_state.assert_called_once()
        self.console.input.assert_not_called()
        rendered = repr(menus) + self.output.getvalue()
        self.assertNotIn('key-a', rendered);self.assertNotIn('key-b', rendered)

    async def test_cancel_does_not_change_any_configuration(self):
        self.tui.choose_menu.return_value = ''
        original = copy.deepcopy(self.cfg)
        await _interactive_model_switch(self.engine, self.tui)
        self.assertEqual(self.cfg, original)
        self.assertEqual(self.updates, [])
        self.engine.switch_model_runtime.assert_not_called()
        self.console.input.assert_not_called()

    async def test_back_then_manual_configuration_remains_available(self):
        self.tui.choose_menu.side_effect = ['saved:a', '', 'configure', '']
        await _interactive_model_switch(self.engine, self.tui)
        self.assertEqual(self.tui.choose_menu.call_count, 4)
        self.assertEqual(self.tui.choose_menu.call_args.kwargs['title'], 'Configure Model')
        self.engine.switch_model_runtime.assert_not_called()

    async def test_empty_catalog_keeps_first_time_configuration(self):
        self.cfg = {'model_connections': []}
        self.tui.choose_menu.return_value = ''
        await _interactive_model_switch(self.engine, self.tui)
        self.assertEqual(self.tui.choose_menu.call_args.kwargs['title'], 'Configure Model')

    async def test_running_children_prevent_switch_before_config_changes(self):
        self.engine.tool_context['subagent_tasks'].tasks['child'] = NS(done=lambda: False)
        self.tui.choose_menu.side_effect = ['saved:b', 'gemini-model']
        await _interactive_model_switch(self.engine, self.tui)
        self.assertEqual(self.updates, [])
        self.engine.switch_model_runtime.assert_not_called()

    async def test_duplicate_model_names_keep_the_selected_api_identity(self):
        self.tui.choose_menu.side_effect = ['saved:b', 'shared']
        await _interactive_model_switch(self.engine, self.tui)
        self.assertEqual(self.engine.switch_model_runtime.call_args.kwargs['api_key'], 'key-b')
        self.assertEqual(self.engine.config.model_connection_id, 'b')

    async def test_current_model_removed_during_menu_is_handled_without_a_runtime_switch(self):
        def choose(**kwargs):
            if kwargs['title'] == 'Select Saved API':
                return 'saved:b'
            self.cfg['model_connections'] = [self.cfg['model_connections'][0]]
            return 'gemini-model'
        self.tui.choose_menu.side_effect = choose
        await _interactive_model_switch(self.engine, self.tui)
        self.engine.switch_model_runtime.assert_not_called()
        self.assertIn('Model switch failed', self.output.getvalue())
        self.assertEqual(self.updates, [])

    async def test_real_engine_saves_selection_and_reasoning_without_http_requests(self):
        import httpx
        from xuloom import EngineConfig, QueryEngine
        calls = []
        async def refuse(request):
            calls.append(request)
            raise AssertionError('Switching saved models must not send any request')
        def client(*args, **kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(refuse))
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {
                'XULOOM_DATA_DIR': temporary + '/data', 'XULOOM_CONFIG_DIR': temporary + '/config',
                'HOME': temporary + '/home', 'USERPROFILE': temporary + '/home'}), \
                patch('xuloom.core.config.load_config', side_effect=lambda **kwargs: copy.deepcopy(self.cfg)), \
                patch('xuloom.core.network.model_http_client', side_effect=client):
            async with QueryEngine(EngineConfig(model='shared', model_provider='openai', api_base_url='https://gateway.invalid/v1',
                    api_key='key-a', cwd=temporary, network_mode='direct', model_connection_id='a', bundled_plugins=False)) as engine:
                self.tui.choose_menu.side_effect = ['saved:b', 'gemini-model']
                await _interactive_model_switch(engine, self.tui)
                self.assertEqual(engine.primary_model, 'gemini-model')
                self.assertEqual(engine.config.model_connection_id, 'b')
                self.assertEqual(engine.tool_context['pricing_connection_id'], 'b')
                self.assertEqual(engine.reasoning_profile['effort'], 'high')
                self.assertEqual(engine._build_session_metadata()['model_selection']['model'], 'gemini-model')
                self.assertTrue(engine.last_persist_ok)
            self.assertTrue(list((Path(temporary) / 'data/history').glob('*/sessions/*.json')))
        self.assertEqual(calls, [])

    async def test_actual_cli_accepts_menu_numbers_without_manual_credentials_or_model_calls(self):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                calls.append(self.path)
                self.send_response(500);self.end_headers()
            do_GET = do_POST
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary);(root / 'config').mkdir();(root / 'work').mkdir()
                cfg = copy.deepcopy(self.cfg)
                for row in cfg['model_connections']:
                    row['base_url'] = f'http://127.0.0.1:{server.server_port}/v1'
                cfg = select_model(cfg, 'a', 'shared')
                (root / 'config/config.json').write_text(json.dumps(cfg), encoding='utf-8')
                env = {k: v for k, v in os.environ.items() if not k.startswith(('XULOOM_', 'OPENAI_', 'ANTHROPIC_', 'GIT_CONFIG_'))}
                env.update(XULOOM_CONFIG_DIR=str(root / 'config'), XULOOM_DATA_DIR=str(root / 'data'),
                           HOME=str(root / 'home'), USERPROFILE=str(root / 'home'),
                           PYTHONPATH=str(Path(__file__).resolve().parents[1]), PYTHONIOENCODING='utf-8')
                result = await asyncio.to_thread(subprocess.run, [sys.executable, '-X', 'utf8', '-m', 'xuloom'],
                    cwd=root / 'work', env=env, input='/model\n2\n2\n/model status\n/quit\n',
                    text=True, encoding='utf-8', capture_output=True, timeout=45)
                self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                self.assertIn('Select Saved API', result.stdout)
                self.assertIn('Select Model', result.stdout)
                self.assertIn('Model switched to gemini-model.', result.stdout)
                self.assertNotIn('Model name (current:', result.stdout)
                self.assertNotIn('key-a', result.stdout);self.assertNotIn('key-b', result.stdout)
                saved = json.loads((root / 'config/config.json').read_text(encoding='utf-8'))
                self.assertEqual(saved['active_model_connection'], 'b')
                self.assertEqual(saved['openai_model'], 'gemini-model')
            self.assertEqual(calls, [])
        finally:
            server.shutdown();server.server_close();thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
