import json
import os
import unittest
from unittest.mock import patch

import httpx

from xuloom.core.model_catalog import save_connection, delete_connection, public_config, select_model, discover_models, sync_legacy_connection, apply_connection_env, active_connection, connections


class ModelCatalogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.legacy = {"model_provider": "openai", "openai_model": "one", "openai_api_key": "first-secret",
                       "openai_base_url": "https://first.invalid/v1", "unrelated": "preserve"}

    def configured(self):
        return save_connection(self.legacy, {"provider": "openai", "model": "one", "models": ["one", "two", " two "],
            "baseUrl": self.legacy["openai_base_url"], "connectionName": "First"})

    def test_migrate_legacy_preserve_key_normalize_models_and_redact_every_connection(self):
        cfg = self.configured()
        self.assertEqual(cfg["openai_models"], ["one", "two"])
        self.assertEqual(cfg["unrelated"], "preserve")
        self.assertEqual(cfg["openai_api_key"], "first-secret")
        self.assertNotIn("first-secret", json.dumps(public_config(cfg)))
        self.assertEqual(public_config(cfg)["models"], ["one", "two"])
        self.assertNotIn("model_connections", self.legacy)

    def test_two_apis_keep_separate_keys_and_switch_projects_legacy_fields(self):
        cfg = self.configured()
        cfg = save_connection(cfg, {"connectionId": "second", "connectionName": "Second", "provider": "openai",
            "model": "other", "models": ["other", "another"], "baseUrl": "https://second.invalid/v1", "apiKey": "second-secret"})
        cfg = select_model(cfg, "legacy-openai", "two")
        self.assertEqual(cfg["openai_api_key"], "first-secret")
        self.assertEqual(cfg["openai_base_url"], "https://first.invalid/v1")
        self.assertEqual(cfg["openai_model"], "two")
        cfg = select_model(cfg, "second", "another")
        self.assertEqual(cfg["openai_api_key"], "second-secret")
        self.assertEqual(len(public_config(cfg)["connections"]), 2)
        self.assertNotIn("second-secret", json.dumps(public_config(cfg)))
        with self.assertRaises(ValueError):
            select_model(cfg, "legacy-openai", "another")
        with self.assertRaises(ValueError):
            save_connection(cfg, {"connectionId": "new", "provider": "openai", "model": "model", "baseUrl": "https://third.invalid/v1"})
        with self.assertRaises(ValueError):
            save_connection(cfg, {"connectionId": "legacy-openai", "provider": "anthropic", "model": "a", "baseUrl": "https://third.invalid/v1"})

    def test_cli_wizard_changes_keep_the_saved_model_list(self):
        cfg = self.configured()
        cfg["openai_model"] = "three"
        cfg = sync_legacy_connection(cfg)
        self.assertEqual(public_config(cfg)["models"], ["one", "two", "three"])
        self.assertEqual(public_config(cfg)["model"], "three")
        apply_connection_env(active_connection(cfg))
        self.assertEqual(os.environ["OPENAI_MODEL"], "three")
        from xuloom.core.config import load_config_into_env
        with patch.dict(os.environ, {}, clear=True), patch("xuloom.core.config.load_config", return_value={
            "model_connections": cfg["model_connections"], "active_model_connection": cfg["active_model_connection"]
        }):
            load_config_into_env()
            self.assertEqual(os.environ["OPENAI_MODEL"], "three")
            self.assertEqual(os.environ["OPENAI_API_KEY"], "first-secret")

    def test_delete_active_falls_back_and_inactive_preserves_selection_and_keys(self):
        first = self.configured()
        cfg = save_connection(first, {"connectionId": "second", "provider": "anthropic", "model": "a", "models": ["a", "b"],
            "baseUrl": "https://second.invalid/v1", "apiKey": "second-secret"})
        original = json.dumps(cfg)
        removed = delete_connection(cfg, 'second')
        self.assertEqual(removed['active_model_connection'], 'legacy-openai')
        self.assertEqual(removed['openai_api_key'], 'first-secret')
        self.assertNotIn('second-secret', json.dumps(removed))
        self.assertNotIn('anthropic_api_key', removed)
        self.assertEqual(json.dumps(cfg), original)
        inactive = delete_connection(cfg, 'legacy-openai')
        self.assertEqual(inactive['active_model_connection'], 'second')
        self.assertEqual(inactive['anthropic_api_key'], 'second-secret')
        self.assertNotIn('first-secret', json.dumps(inactive))
        with self.assertRaises(ValueError):
            delete_connection(cfg, 'missing')
        self.assertEqual(json.dumps(cfg), original)

    def test_delete_last_stays_empty_across_environment_and_cli_reload_and_can_readd(self):
        cfg = delete_connection(self.configured(), 'legacy-openai')
        self.assertEqual(cfg['model_connections'], [])
        self.assertEqual(cfg['unrelated'], 'preserve')
        self.assertNotIn('first-secret', json.dumps(cfg))
        with patch.dict(os.environ, {'OPENAI_MODEL': 'stale', 'OPENAI_API_KEY': 'first-secret', 'ANTHROPIC_API_KEY': 'other-secret', 'XULOOM_OPENAI_MODEL': 'stale-alias'}):
            self.assertEqual(connections(cfg), [])
            self.assertEqual(public_config(cfg)['model'], '')
            self.assertFalse(public_config(cfg)['hasKey'])
            from xuloom.core.config import load_config_into_env
            with patch('xuloom.core.config.load_config', return_value=cfg):
                load_config_into_env()
            for key in ('OPENAI_MODEL', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'XULOOM_OPENAI_MODEL'):
                self.assertNotIn(key, os.environ)
        restored = save_connection(cfg, {'provider': 'openai', 'model': 'new', 'apiKey': 'new-secret'})
        self.assertEqual(public_config(restored)['model'], 'new')
        self.assertEqual(len(connections(restored)), 1)

    async def test_discovery_uses_own_key_filters_deduplicates_and_does_not_save(self):
        cfg = self.configured()
        original = json.dumps(cfg)
        async def handler(request):
            self.assertEqual(str(request.url), "https://first.invalid/v1/models")
            self.assertEqual(request.headers["authorization"], "Bearer first-secret")
            return httpx.Response(200, json={"data": [{"id": "one"}, {"id": "new", "type": "chat"}, {"id": "one"}, {"id": "embed", "type": "embedding"}]})
        factory = httpx.AsyncClient
        with patch("xuloom.core.network.model_http_client", side_effect=lambda **kwargs: factory(transport=httpx.MockTransport(handler), timeout=kwargs["timeout"])):
            result = await discover_models(cfg, {"connectionId": "legacy-openai", "baseUrl": "https://first.invalid/v1/chat/completions"})
        self.assertEqual(result["models"], ["one", "new"])
        self.assertEqual(json.dumps(cfg), original)
        with self.assertRaises(ValueError):
            await discover_models(cfg, {"connectionId": "legacy-openai", "baseUrl": "https://another.invalid/v1"})

    async def test_anthropic_pagination_and_errors_hide_credentials(self):
        cfg = save_connection(self.legacy, {"connectionId": "anthropic-test", "provider": "anthropic", "model": "a", "models": ["a"],
            "baseUrl": "https://anthropic.invalid/v1", "apiKey": "anthropic-secret"})
        calls = []
        async def handler(request):
            calls.append(request)
            self.assertEqual(request.headers["x-api-key"], "anthropic-secret")
            return httpx.Response(200, json={"data": [{"id": "b" if len(calls) == 2 else "a"}], "has_more": len(calls) == 1, "last_id": "a"})
        factory = httpx.AsyncClient
        with patch("xuloom.core.network.model_http_client", side_effect=lambda **kwargs: factory(transport=httpx.MockTransport(handler), timeout=kwargs["timeout"])):
            self.assertEqual((await discover_models(cfg, {"connectionId": "anthropic-test"}))["models"], ["a", "b"])
        self.assertEqual(calls[1].url.params["after_id"], "a")
        with patch("xuloom.core.network.model_http_client", side_effect=lambda **kwargs: factory(transport=httpx.MockTransport(lambda request: httpx.Response(401, text="anthropic-secret")), timeout=kwargs["timeout"])):
            with self.assertRaises(ValueError) as error:
                await discover_models(cfg, {"connectionId": "anthropic-test"})
        self.assertNotIn("anthropic-secret", str(error.exception))
