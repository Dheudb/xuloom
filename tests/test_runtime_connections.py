"""Actual SDK requests to local fixtures; no external endpoints or user keys."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from xuloom.core.engine import QueryEngine
from xuloom.core.network import model_http_client, openai_credentials
from xuloom.memory.memory import MemoryCompactor


class RuntimeConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xuloom connections ")
        self.root = Path(self.temp.name)
        self.records, self.engines = [], []
        self.env = patch.dict(os.environ, {
            "XULOOM_CONFIG_DIR": str(self.root / "config"), "XULOOM_DATA_DIR": str(self.root / "data"),
            "HOME": str(self.root / "home"), "USERPROFILE": str(self.root / "home"),
            "OPENAI_API_KEY": "ambient-openai", "ANTHROPIC_API_KEY": "ambient-anthropic",
            "ANTHROPIC_AUTH_TOKEN": "ambient-bearer", "ANTHROPIC_PROFILE": "never-load-this-profile",
            "ANTHROPIC_CUSTOM_HEADERS": "X-Api-Key: ambient-header\nAuthorization: Bearer ambient-header\nX-Private-Tenant: ambient-tenant\nContent-Type: ambient-type\nanthropic-version: ambient-version",
            "OPENAI_ORG_ID": "ambient-org", "OPENAI_PROJECT_ID": "ambient-project",
            "OPENAI_ADMIN_KEY": "ambient-admin",
            "OPENAI_CUSTOM_HEADERS": "Authorization: Bearer ambient-openai-header\nX-Private-Tenant: ambient-openai-tenant\nOpenAI-Organization: ambient-header-org\nContent-Type: ambient-type",
            "OPENAI_BASE_URL": "http://127.0.0.1:1/unintended", "ANTHROPIC_BASE_URL": "http://127.0.0.1:1/unintended",
            "XULOOM_NETWORK_MODE": "direct", "HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": "", "NO_PROXY": "*",
        })
        self.env.start()
        self.tokens = patch.object(MemoryCompactor, "_resolve_token_encoder", return_value=None); self.tokens.start()
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                custom=getattr(owner,'web_handler',None)
                if custom is not None and custom(self):return
                self.send_error(404)
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.records.append({"path": self.path, "headers": dict(self.headers), "body": body})
                custom = getattr(owner, "response_handler", None)
                if custom is not None and custom(self, body): return
                if self.path.startswith("/redirect/"):
                    self.send_response(307); self.send_header("Location", owner.base + "/trap/v1/messages"); self.end_headers(); return
                label = self.path.split("/")[1]
                if body.get("stream"):
                    self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
                    frames = [{"choices": [{"index": 0, "delta": {"role": "assistant", "content": "answer-" + label}, "finish_reason": None}]},
                              {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]
                    self.wfile.write(("".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n").encode()); return
                result = {"id": "fixture", "model": body["model"], "usage": {"input_tokens": 1, "output_tokens": 1}}
                if self.path.endswith("/messages"):
                    result.update(type="message", role="assistant", content=[{"type": "text", "text": "answer-" + label}], stop_reason="end_turn", stop_sequence=None)
                else:
                    result.update(object="chat.completion", choices=[{"index": 0, "message": {"role": "assistant", "content": "answer-" + label}, "finish_reason": "stop"}])
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(json.dumps(result).encode())
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = "http://127.0.0.1:" + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()

    async def asyncTearDown(self):
        await asyncio.gather(*(engine.aclose() for engine in self.engines))
        await asyncio.to_thread(self.server.shutdown); self.server.server_close(); self.thread.join(timeout=5)
        self.tokens.stop(); self.env.stop(); self.temp.cleanup()

    def engine(self, label, provider="openai", key=None, mode="direct"):
        engine = QueryEngine(model="opaque-model", model_provider=provider, api_base_url=self.base + "/" + label + ("/v1" if provider == "openai" else ""),
            api_key=key if key is not None else "key-" + label, network_mode=mode, cwd=str(self.root))
        self.engines.append(engine)
        return engine

    async def request(self, engine, *, compactor=False):
        client = engine.compactor.client if compactor else engine.client
        if engine.model_provider == "openai":
            return await client.chat.completions.create(model=engine.primary_model, messages=[{"role": "user", "content": "hello"}])
        return await client.messages.create(model=engine.primary_model, messages=[{"role": "user", "content": "hello"}], max_tokens=20)

    def assert_credentials(self, record, label, provider):
        headers = {key.lower(): value for key, value in record["headers"].items()}
        self.assertEqual(record["path"].split("/")[1], label)
        if provider == "openai":
            self.assertEqual(headers["authorization"], "Bearer key-" + label)
            self.assertNotIn("openai-organization", headers); self.assertNotIn("openai-project", headers)
            self.assertNotIn("x-private-tenant", headers)
            self.assertEqual(headers["content-type"], "application/json")
        else:
            self.assertEqual(headers["x-api-key"], "key-" + label)
            self.assertNotIn("authorization", headers); self.assertNotIn("x-private-tenant", headers)
            self.assertEqual(headers["content-type"], "application/json")
            self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertNotIn("ambient-", json.dumps(headers))

    async def test_concurrent_providers_and_compactors_keep_independent_bindings(self):
        first, second = self.engine("a"), self.engine("b", "anthropic", mode="system")
        self.assertFalse(first.client._client.trust_env); self.assertTrue(second.client._client.trust_env)
        self.assertFalse(first.compactor.client._client.trust_env); self.assertTrue(second.compactor.client._client.trust_env)
        await asyncio.gather(self.request(first), self.request(second), self.request(first, compactor=True), self.request(second, compactor=True))
        self.assertEqual(len(self.records), 4)
        for record in self.records:
            label = record["path"].split("/")[1]
            self.assert_credentials(record, label, "openai" if label == "a" else "anthropic")

    async def test_hot_switch_does_not_mutate_global_credentials_or_other_engines(self):
        first, second = self.engine("a"), self.engine("b", "anthropic")
        environment = dict(os.environ)
        old = [first.client, first.compactor.client]
        result = first.switch_model_runtime(provider="openai", model="other", api_base_url=self.base + "/c/v1", api_key="key-c", network_mode="direct", persist=False)
        await asyncio.gather(*(client.close() for client in old))
        self.assertTrue(result["has_api_key"]); self.assertEqual(environment, dict(os.environ))
        await asyncio.gather(self.request(first), self.request(second), self.request(first, compactor=True))
        for record in self.records:
            label = record["path"].split("/")[1]
            self.assert_credentials(record, label, "anthropic" if label == "b" else "openai")

    async def test_native_worker_uses_parent_binding_not_later_environment_changes(self):
        parent = self.engine("a")
        parent.permission_manager.approval_policy.set_mode("full")
        parent.set_collaboration_mode("ultra")
        os.environ["OPENAI_API_KEY"] = "changed-ambient-key"
        os.environ["XULOOM_NETWORK_MODE"] = "system"
        result = await parent.agents("spawn", prompt="Reply with a short greeting", run_in_background=False)
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(self.records), 1); self.assert_credentials(self.records[0], "a", "openai")
        state = json.dumps(parent._build_session_metadata(), default=str)
        self.assertNotIn("key-a", state); self.assertNotIn("ambient-", state)
        for path in (self.root / "data/history").rglob("*.json"):
            self.assertNotIn("key-a", path.read_text(encoding="utf-8"))

    async def test_history_cannot_redirect_current_credentials_to_saved_endpoint(self):
        engine = self.engine("a")
        engine.require_writer()
        raw = engine._build_session_metadata()
        raw["model_runtime"].update(provider="anthropic", api_base_url=self.base + "/trap", primary_model="old-model")
        self.assertTrue(engine.session_manager.save_session_state(engine.session_id, [{"role": "user", "content": "saved"}], raw))
        old = [engine.client, engine.compactor.client]
        self.assertTrue(engine.load_session(engine.session_id))
        await asyncio.gather(*(client.close() for client in old))
        self.assertEqual(engine.model_provider, "openai"); self.assertTrue(engine.api_base_url.endswith("/a/v1"))
        await self.request(engine); self.assert_credentials(self.records[0], "a", "openai")

    async def test_redirects_do_not_forward_credentials_and_explicit_empty_key_stays_empty(self):
        engine = self.engine("redirect", "anthropic")
        from anthropic import APIStatusError
        with self.assertRaises(APIStatusError): await self.request(engine)
        self.assertEqual(len(self.records), 1); self.assertTrue(self.records[0]["path"].startswith("/redirect/"))
        from openai import OpenAIError
        try:
            blank = self.engine("blank", key="")
        except OpenAIError as error:
            self.assertIn("credential", str(error).lower())
        else:
            self.assertEqual(blank.client.api_key, "")
            self.assertEqual(blank.compactor.client.api_key, "")
            self.assertFalse(getattr(blank.client, "admin_api_key", ""))
        with self.assertRaises(ValueError): model_http_client(network_mode="unknown")

    async def test_minimum_openai_client_without_project_parameter_still_gets_explicit_credentials(self):
        class OldSDK:
            def __init__(self, api_key=None, organization=None, default_headers=None): pass
        options = openai_credentials("fixture", OldSDK)
        self.assertEqual(options["api_key"], "fixture"); self.assertEqual(options["organization"], "")
        self.assertNotIn("project", options)

    async def test_connection_switch_with_pending_workers_is_rejected_before_mutation(self):
        engine = self.engine("a")
        tasks = engine.tool_context["subagent_tasks"]
        tasks.launch("owned-test", asyncio.Event().wait())
        original_client = engine.client
        with self.assertRaisesRegex(ValueError, "owned workers"):
            engine.switch_model_runtime(api_base_url=self.base + "/b/v1", api_key="key-b", persist=False)
        self.assertIs(engine.client, original_client)
        self.assertTrue(engine.api_base_url.endswith("/a/v1"))

    async def test_failed_switch_keeps_live_binding_history_and_cleans_staged_clients(self):
        engine = self.engine("a")
        before = dict(os.environ)
        original = engine.client
        engine.messages = [{"role": "assistant", "content": [{"type": "thinking", "thinking": "old", "signature": "keep"}]}]
        with patch("xuloom.core.network.create_model_client", side_effect=ValueError("fixture construction failure")):
            with self.assertRaisesRegex(ValueError, "construction failure"):
                engine.switch_model_runtime(provider="anthropic", api_base_url=self.base + "/b", api_key="key-b", persist=False)
        self.assertIs(engine.client, original); self.assertEqual(engine.model_provider, "openai")
        self.assertEqual(engine.messages[0]["content"][0]["signature"], "keep")
        with patch("xuloom.core.engine.MemoryCompactor", side_effect=ValueError("fixture compactor failure")):
            with self.assertRaisesRegex(ValueError, "compactor failure"):
                engine.switch_model_runtime(api_base_url=self.base + "/c/v1", api_key="key-c", persist=False)
        self.assertIs(engine.client, original); self.assertEqual(before, dict(os.environ))
        staged = engine._retired_model_clients[-1]
        await engine.aclose(); self.assertTrue(staged.is_closed)

    async def test_explicit_gateway_model_ids_and_worker_overrides_are_not_rewritten(self):
        engine = QueryEngine(model="claude-gateway", fallback_model="claude-compress", model_provider="openai",
            api_base_url=self.base + "/a/v1", api_key="key-a", network_mode="direct", cwd=str(self.root))
        self.engines.append(engine)
        self.assertEqual(engine.primary_model, "claude-gateway")
        self.assertEqual(engine.fallback_model, "claude-compress")
        self.assertEqual(engine.compactor.model, "claude-compress")
        engine.permission_manager.approval_policy.set_mode("full"); engine.set_collaboration_mode("ultra")
        result = await engine.agents("spawn", prompt="Reply briefly", model="claude-child", run_in_background=False)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.records[0]["body"]["model"], "claude-child")
