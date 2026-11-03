"""Provider contracts over local HTTP transports; never use a real account."""
import asyncio
import json
import unittest
from unittest.mock import patch

import httpx
from xuloom.tools.native_search import normalize, configured, endpoint
from xuloom.tools.web_tool import WebSearchTool, WebFetchTool
from xuloom.core.model_catalog import save_connection, public_config


class NativeSearchTests(unittest.IsolatedAsyncioTestCase):
    def context(self, backend, base='https://fixture.invalid/v1'):
        self.context_value = {'session_token_usage': {'input_tokens': 1, 'output_tokens': 2}, 'token_usage_history': [],
            'native_search_options': lambda: {'profile': normalize({'backend': backend}), 'model': 'fixture-model',
                'base_url': base, 'api_key': 'fixture-key', 'network_mode': 'direct', 'owner': 'fixture-owner'}}
        return self.context_value

    async def call(self, backend, responses, fetch=False):
        seen = []
        def handle(request):
            seen.append(request)
            body = responses.pop(0)
            return httpx.Response(200, json=body)
        tool = WebFetchTool() if fetch else WebSearchTool()
        tool.context = self.context(backend)
        with patch('xuloom.core.network.model_http_client', side_effect=lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=False)), patch('xuloom.tools.web_tool.DDGS') as ddgs:
            result = await (tool.execute('https://evidence.invalid/page') if fetch else tool.execute('question', 15))
            ddgs.assert_not_called()
        return result, seen

    async def test_native_protocols_urls_headers_sources_and_usage(self):
        url = 'https://evidence.invalid/paper'
        for backend in ('openai', 'qwen', 'openai-chat', 'anthropic', 'deepseek'):
            with self.subTest(backend=backend):
                if backend in {'openai', 'qwen'}:
                    body = {'output': [{'type': 'web_search_call', 'action': {'sources': [{'url': url, 'title': 'Paper'}]}},
                        {'type': 'message', 'content': [{'type': 'output_text', 'text': 'Finding', 'annotations': []}]}]}
                elif backend == 'anthropic':
                    body = {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'Finding', 'citations': [{'url': url, 'title': 'Paper'}]}]}
                elif backend == 'deepseek':
                    body = {'results': [{'url': url, 'title': 'Paper', 'summary': 'Finding'}]}
                else:
                    body = {'choices': [{'message': {'content': 'Finding', 'annotations': [{'url_citation': {'url': url, 'title': 'Paper'}}]}}]}
                body['usage'] = {'input_tokens': 7, 'output_tokens': 3}
                result, seen = await self.call(backend, [body])
                self.assertTrue(result['ok'], result)
                self.assertIn('[Paper](' + url + ')', result['content'])
                self.assertEqual(result['metadata']['sources'][0]['url'], url)
                request = seen[0]
                self.assertEqual(request.url.host, 'fixture.invalid')
                payload = json.loads(request.content)
                if backend == 'anthropic':
                    self.assertEqual(request.headers['x-api-key'], 'fixture-key')
                    self.assertNotIn('authorization', request.headers)
                    self.assertEqual(payload['tools'][0]['type'], 'web_search_20250305')
                    self.assertEqual(request.url.path, '/v1/messages')
                else:
                    self.assertEqual(request.headers['authorization'], 'Bearer fixture-key')
                if backend == 'deepseek':
                    self.assertEqual(request.url.path, '/v1/tools/search')
                    self.assertEqual(payload, {'query': 'question', 'count': 15, 'need_content': False})
                elif backend in {'openai', 'qwen'}:
                    self.assertEqual(request.url.path, '/v1/responses')
                    self.assertEqual(payload['tools'], [{'type': 'web_search'}])
                elif backend == 'openai-chat':
                    self.assertEqual(payload['web_search_options'], {})
                self.assertEqual(self.context_value['session_token_usage']['output_tokens'], 5)
                self.assertEqual(self.context_value['token_usage_history'][0]['source'], 'native_search')

    async def test_native_model_search_uses_existing_output_grants(self):
        from xuloom.features.goals import CURRENT_BUDGET
        budget = {'budget': {'output_tokens': 10}, 'output_used': 0, 'output_reserved': 0}
        token = CURRENT_BUDGET.set(budget)
        try:
            result, requests = await self.call('openai', [{'output': [{'type': 'web_search_call', 'action': {'sources': [{'url': 'https://evidence.invalid'}]}}],
                'usage': {'input_tokens': 7, 'output_tokens': 3, 'cache_read_input_tokens': 4}}])
            self.assertTrue(result['ok'])
            self.assertEqual(json.loads(requests[0].content)['max_output_tokens'], 10)
            self.assertEqual(budget['output_used'], 3)
            self.assertEqual(budget['output_reserved'], 0)
            self.assertEqual(self.context_value['session_token_usage']['cache_read_input_tokens'], 4)
            budget['output_used'] = 10
            result, requests = await self.call('deepseek', [{'results': [{'url': 'https://evidence.invalid'}]}])
            self.assertTrue(result['ok'], 'Direct Search/Fetch uses no auxiliary model output grant')
            self.assertEqual(budget['output_used'], 10)
        finally:
            CURRENT_BUDGET.reset(token)

    async def test_anthropic_pause_resumes_with_real_content(self):
        first = {'stop_reason': 'pause_turn', 'content': [{'type': 'server_tool_use', 'id': 'srv', 'name': 'web_search', 'input': {'query': 'question'}}], 'usage': {'input_tokens': 2}}
        last = {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'Answer', 'citations': [{'url': 'https://evidence.invalid', 'title': 'Evidence'}]}]}
        result, requests = await self.call('anthropic', [first, last])
        self.assertTrue(result['ok'])
        self.assertEqual(json.loads(requests[1].content)['messages'][-1], {'role': 'assistant', 'content': first['content']})
        self.assertEqual(len(result['metadata']['usage']), 2)

    async def test_missing_sources_and_truncated_answers_do_not_claim_success(self):
        result, requests = await self.call('openai', [{'output': [{'content': [{'type': 'output_text', 'text': 'Ungrounded guess'}]}]}])
        self.assertFalse(result['ok'])
        self.assertEqual(result['metadata']['code'], 'no_grounding')
        self.assertEqual(len(requests), 1)
        result, _ = await self.call('openai', [{'status': 'incomplete', 'output': [{'type': 'web_search_call', 'action': {'sources': [{'url': 'https://evidence.invalid'}]}}]}])
        self.assertFalse(result['ok'])
        self.assertEqual(result['metadata']['code'], 'search_incomplete')
        self.assertEqual(len(result['metadata']['sources']), 1)

    async def test_deepseek_fetch_and_query_limit(self):
        result, requests = await self.call('deepseek', [{'content': '# Page', 'title': 'Page'}], fetch=True)
        self.assertTrue(result['ok'])
        self.assertEqual(requests[0].url.path, '/v1/tools/fetch')
        self.assertEqual(json.loads(requests[0].content), {'url': 'https://evidence.invalid/page'})
        tool = WebSearchTool(); tool.context = self.context('deepseek')
        with patch('xuloom.core.network.model_http_client') as client:
            result = await tool.execute('x' * 129)
            self.assertEqual(result['metadata']['code'], 'invalid_query')
            client.assert_not_called()

    async def test_redirect_failure_never_forwards_credentials_or_falls_back(self):
        seen = []
        def handle(request):
            seen.append(request)
            return httpx.Response(302, headers={'Location': 'https://other.invalid/stolen'})
        tool = WebSearchTool(); tool.context = self.context('openai')
        with patch('xuloom.core.network.model_http_client', side_effect=lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=False)), patch('xuloom.tools.web_tool.DDGS') as ddgs:
            result = await tool.execute('question')
            self.assertFalse(result['ok'])
            self.assertEqual(result['metadata']['http_status'], 302)
            self.assertEqual(len(seen), 1)
            ddgs.assert_not_called()
            self.assertNotIn('fixture-key', json.dumps(result))

    async def test_rate_limit_and_cancellation_are_not_retried(self):
        for code in (429, 401):
            requests = []
            def handle(request):
                requests.append(request)
                return httpx.Response(code, json={'error': 'fixture'})
            tool = WebSearchTool(); tool.context = self.context('openai')
            with patch('xuloom.core.network.model_http_client', side_effect=lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(handle))):
                result = await tool.execute('question')
                self.assertFalse(result['ok'])
                self.assertEqual(result['metadata']['http_status'], code)
                self.assertEqual(result['metadata']['retryable'], code == 429)
                self.assertEqual(len(requests), 1)
        entered = asyncio.Event()
        async def hold(request):
            entered.set()
            await asyncio.Event().wait()
        with patch('xuloom.core.network.model_http_client', side_effect=lambda **_: httpx.AsyncClient(transport=httpx.MockTransport(hold))):
            task = asyncio.create_task(tool.execute('question'))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)

    def test_connection_selection_is_not_guessed_from_model_name(self):
        cfg = {'active_model_connection': 'first', 'model_connections': [
            {'id': name, 'provider': 'openai', 'model': 'model', 'models': ['model'], 'base_url': 'https://fixture.invalid/v1',
                'api_key': key, 'native_search': {'backend': backend}}
            for name, key, backend in [('first', 'first-key', 'deepseek'), ('second', 'second-key', 'openai')]]}
        with patch('xuloom.core.config.load_config', return_value=cfg):
            self.assertEqual(configured('openai', 'model', 'https://fixture.invalid/v1', 'second-key')['backend'], 'openai')
            self.assertEqual(configured('openai', 'deepseek-named-but-unconfigured', 'https://fixture.invalid/v1', 'second-key')['backend'], 'builtin')

    def test_settings_preserve_search_and_freeze_worker_contract(self):
        params = {'connectionId': 'fixture', 'provider': 'openai', 'model': 'model', 'baseUrl': 'https://fixture.invalid/v1',
            'apiKey': 'fixture-key', 'nativeSearch': {'backend': 'openai', 'model': 'search-model'}}
        cfg = save_connection({}, params)
        params.pop('nativeSearch')
        cfg = save_connection(cfg, params)
        self.assertEqual(public_config(cfg)['nativeSearch']['model'], 'search-model')
        self.assertNotIn('fixture-key', json.dumps(public_config(cfg)))
        from xuloom.agents.connections import resolve_connection
        with patch('xuloom.agents.connections.load_config', return_value=cfg):
            binding, private = resolve_connection('fixture')
            self.assertEqual(binding['contract']['search']['backend'], 'openai')
            self.assertNotIn('fixture-key', json.dumps(binding))
            next(row for row in cfg['model_connections'] if row['id'] == 'fixture')['native_search']['backend'] = 'deepseek'
            with self.assertRaisesRegex(ValueError, 'changed'):
                resolve_connection('fixture', frozen=binding)
        for value in ({'backend': 'unknown'}, {'baseUrl': 'https://key:secret@wrong.invalid'}, {'baseUrl': 'https://wrong.invalid?key=secret'}):
            with self.assertRaises(ValueError): normalize(value)
        self.assertEqual(endpoint('https://fixture.invalid', '/tools/search'), 'https://fixture.invalid/v1/tools/search')

    async def test_cli_search_configuration_uses_existing_connection_and_emits_no_key(self):
        from xuloom.cli import parser, dispatch
        cfg = save_connection({}, {'connectionId': 'fixture', 'provider': 'openai', 'model': 'model', 'baseUrl': 'https://fixture.invalid/v1', 'apiKey': 'fixture-key'})
        def update(fn): return fn(cfg)
        events = []
        async def emit(event): events.append(event)
        with patch('xuloom.core.config.update_config', side_effect=update):
            result = await dispatch(parser().parse_args(['search-config', '--backend', 'deepseek', '--json']), emit)
        self.assertTrue(result['ok'])
        self.assertEqual(result['search']['backend'], 'deepseek')
        self.assertNotIn('fixture-key', json.dumps([result, events]))
