import unittest
from unittest.mock import patch

from xuloom.core.engine_config import resolve_primary_model, resolve_fallback_model
from xuloom.core.model_catalog import save_connection, connections, public_config
from xuloom.models import ModelAdapter, register_adapter, unregister_adapter


class CatalogAdapter(ModelAdapter):
    name = 'catalog-test'

    async def stream_turn(self, request):
        raise AssertionError('No network in catalog tests')


class AdapterCatalogTests(unittest.TestCase):
    def test_custom_connection_roundtrip_and_secret_redaction(self):
        register_adapter(CatalogAdapter())
        self.addCleanup(unregister_adapter, 'catalog-test')
        with patch('xuloom.plugins.default_plugin_names', return_value=[]):
            cfg = save_connection({}, dict(provider='catalog-test', model='local', apiKey='secret-example',
                                          baseUrl='http://localhost:1234', connectionId='custom'))
            value = public_config(cfg)
        self.assertIn('catalog-test', value['adapters'])
        self.assertEqual(value['provider'], 'catalog-test')
        self.assertNotIn('secret-example', str(value))
        unregister_adapter('catalog-test')
        self.assertEqual(next(c for c in connections(cfg) if c['id'] == 'custom')['provider'], 'catalog-test')

    def test_custom_environment_never_inherits_anthropic_model(self):
        env = {'ANTHROPIC_MODEL': 'wrong', 'CATALOG-TEST_MODEL': 'right'}
        self.assertEqual(resolve_primary_model('catalog-test', None, env), 'right')
        self.assertEqual(resolve_fallback_model('catalog-test', None, 'right', env), 'right')
