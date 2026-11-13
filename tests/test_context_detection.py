"""Window detection uses explicit provider metadata, not model-name guesses."""
import copy
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import httpx
from xuloom.context.compaction import policy
from xuloom.core.model_catalog import discover_models
from xuloom.core.model_specs import official, with_defaults


class WindowDefaultsTests(unittest.TestCase):
    def test_published_native_windows_are_nominal_fallbacks_not_fine_tune_guesses(self):
        for name, expected in [('Qwen/Qwen3.6-35B-A3B', 262144), ('Intern-S2-Preview-397B', 262144),
                               ('agents-a1', 262144), ('glm-5.2', 1048576), ('kimi-k2.6', 262144), ('intern-s1', 65536)]:
            engine = NS(primary_model=name, model_provider='openai', api_base_url='https://gateway.invalid/v1', _model_contract={'context': {}})
            with patch.dict('os.environ', {}, clear=True):
                self.assertEqual(policy(engine)['window'], expected)
                self.assertEqual(policy(engine)['source'], 'published_model')
        self.assertNotIn('Sage-A1.5', with_defaults({'provider': 'openai', 'models': ['Sage-A1.5']}).get('model_context', {}))

    def test_deepseek_official_window_without_changing_request_protocol(self):
        for base in ['https://api.deepseek.com', 'https://api.deepseek.com/v1']:
            for name in ['deepseek-flash', 'deepseek-v4-pro']:
                engine = NS(primary_model=name, model_provider='openai', api_base_url=base, _model_contract={'context': {}})
                with patch.dict('os.environ', {}, clear=True):
                    self.assertEqual(policy(engine)['window'], 1048576)
                    self.assertEqual(policy(engine)['source'], 'official_spec')
                self.assertIsNone(official('openai', name, base), 'Context metadata must not force OpenAI Responses transport')
                original = {'provider': 'openai', 'models': [name], 'base_url': base}
                resolved = with_defaults(original)
                self.assertEqual(resolved['model_context'][name]['window'], 1048576)
                self.assertNotIn('model_reasoning', resolved)
                self.assertNotIn('model_inputs', resolved)
                self.assertNotIn('model_context', original)

    def test_explicit_window_and_unknown_gateway_are_preserved(self):
        engine = NS(primary_model='deepseek-flash', model_provider='openai', api_base_url='https://api.deepseek.com',
                    _model_contract={'context': {'window': 128000}})
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual(policy(engine)['window'], 128000)
            engine._model_contract = {'context': {}}
            for base in ['http://api.deepseek.com', 'https://api.deepseek.com.evil/v1', 'https://api.deepseek.com:444/v1',
                         'https://api.deepseek.com/v1?route=other', 'https://gateway.invalid/v1']:
                engine.api_base_url = base
                self.assertEqual(policy(engine)['window'], 65536)

    def test_compatible_anthropic_route_has_only_context_defaults(self):
        item = {'provider': 'anthropic', 'models': ['deepseek-flash'], 'base_url': 'https://api.deepseek.com/anthropic'}
        self.assertEqual(with_defaults(item)['model_context']['deepseek-flash']['window'], 1048576)


class MetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_returns_windows_only_for_actual_chat_model_rows_without_saving(self):
        cfg = {'model_connections': [{'id': 'gateway', 'provider': 'openai', 'base_url': 'https://gateway.invalid/v1',
            'api_key': 'fixture-secret', 'model': 'a', 'models': ['a'], 'model_context': {'a': {'window': 65536}}}]}
        previous = copy.deepcopy(cfg)
        async def respond(request):
            self.assertEqual(request.method, 'GET')
            self.assertEqual(request.url.path, '/v1/models')
            return httpx.Response(200, json={'data': [
                {'id': 'a', 'max_model_len': 1048576, 'api_key': 'fixture-secret'},
                {'id': 'b', 'context_window': 262144},
                {'id': 'input-only', 'max_input_tokens': 128000},
                {'id': 'output-only', 'max_output_tokens': 393216},
                {'id': 'bad', 'context_window': True},
                {'id': 'embed', 'type': 'embedding', 'context_length': 8192}]})
        real = httpx.AsyncClient
        with patch('xuloom.core.network.model_http_client', side_effect=lambda **kw:
                   real(transport=httpx.MockTransport(respond), timeout=kw["timeout"])):
            result = await discover_models(cfg, {'connectionId': 'gateway'})
        self.assertEqual(result['modelContext']['a']['window'], 1048576)
        self.assertEqual(result['modelContext']['b']['window'], 262144)
        self.assertEqual(set(result['modelContext']), {'a', 'b'})
        self.assertNotIn('embed', result['models'])
        self.assertNotIn('fixture-secret', json.dumps(result))
        self.assertEqual(cfg, previous, 'Discovery proposes settings; save remains explicit')

    def test_strict_bounded_metadata_and_conflicts_use_smaller_window(self):
        from xuloom.core.model_specs import metadata_window
        for row in [{}, {'context_window': '1M'}, {'context_window': False}, {'context_length': -1},
                    {'max_output_tokens': 1000000}, {'max_input_tokens': 1000000}, {'context_length': 16000001}]:
            self.assertIsNone(metadata_window(row))
        self.assertEqual(metadata_window({'max_model_len': 1048576}), 1048576)
        self.assertEqual(metadata_window({'top_provider': {'context_length': 262144}}), 262144)
        self.assertEqual(metadata_window({'limits': {'context_window': 131072}}), 131072)
        self.assertEqual(metadata_window({'context_window': 1048576, 'max_model_len': 131072}), 131072)
