"""Deterministic failures and real loopback proxy routing; no public searches/models."""
import asyncio
import json
import os
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from xuloom.tools.web_tool import WebSearchTool
from xuloom.tools.web_network import SearchError, network_error, safe_detail, proxy_for_url

ROWS = [{"href": "https://evidence.invalid/paper", "title": "Evidence", "body": "Source"}]


def tool(mode="direct", abort=None):
    value = WebSearchTool()
    value.context = {"native_search_options": lambda: {"profile": {"backend": "builtin"}, "network_mode": mode,
        "api_key": "private-model-key"}, "abort_event": abort}
    return value


class DiagnosticTests(unittest.TestCase):
    def test_proxy_scheme_selection_bypass_and_bad_configuration(self):
        proxies = {"https":"127.0.0.1:7890", "http":"http://127.0.0.1:7891", "no":"internal.invalid"}
        self.assertEqual(proxy_for_url(proxies,"https://public.invalid"),"http://127.0.0.1:7890")
        self.assertEqual(proxy_for_url(proxies,"http://public.invalid"),"http://127.0.0.1:7891")
        self.assertIsNone(proxy_for_url(proxies,"https://internal.invalid"))
        self.assertIsNone(proxy_for_url({"http":"http://127.0.0.1:7891"},"https://public.invalid"))
        with self.assertRaises(ValueError) as caught:
            proxy_for_url({"https":"http://user:private-password@localhost:private-port"},"https://public.invalid")
        diagnostic = network_error(caught.exception,{})
        self.assertEqual(diagnostic["code"],"configuration_error")
        self.assertNotIn("private-password",diagnostic["detail"])
        self.assertNotIn("private-port",diagnostic["detail"])

    def test_wrapped_causes_statuses_and_unknown_errors_have_distinct_retryability(self):
        cases = [("DNS lookup failed", "dns_error", True), ("connection reset by peer", "connection_error", True),
            ("request timed out", "timeout", True), ("HTTP 429 Too many requests", "rate_limited", True),
            ("HTTP/1.1 503 Service unavailable", "http_error", True), ("HTTP 403 Forbidden", "access_denied", False),
            ("HTTP 407 Proxy authentication required", "proxy_authentication_error", False),
            ("TLS handshake failed: certificate verify failed", "tls_error", False),
            ("No results found.", "no_results", False), ("query is mandatory.", "invalid_query", False),
            ("unknown upstream failure", "provider_error", True)]
        for message, code, retryable in cases:
            with self.subTest(message=message):
                error = SearchError(message)
                result = network_error(error, {})
                self.assertEqual((result["code"], result["retryable"]), (code, retryable))
                self.assertIn(message, result["detail"])
        inner = ConnectionResetError("socket failed")
        error = SearchError("search failed"); error.__cause__ = inner
        result = network_error(error, {})
        self.assertEqual(result["code"], "connection_error")
        self.assertIn("ConnectionResetError", result["detail"])
        self.assertEqual(network_error(SearchError(inner), {})["code"], "connection_error")
        self.assertFalse(network_error(RuntimeError("programming bug"), {})["retryable"])

    def test_details_keep_root_cause_but_never_keys_auth_proxy_passwords_or_signed_queries(self):
        secret = "opaque-private-model-key"
        error = SearchError(f"DNS failed through http://user:proxy-password@localhost:7890/?token=signed-secret#private-fragment Authorization: Bearer {secret}; api_key='another-private-key'")
        result = network_error(error, {}, [secret, "proxy-password"])
        serialized = json.dumps(result)
        for value in [secret, "proxy-password", "user:", "signed-secret", "private-fragment", "another-private-key"]:
            self.assertNotIn(value, serialized)
        self.assertIn("DNS failed", result["detail"])
        self.assertIn("localhost:7890", result["detail"])
        self.assertLessEqual(len(safe_detail("x" * 10000)), 1000)


class SearchRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_retry_recovers_and_retains_first_failure_without_other_backends(self):
        with patch("xuloom.tools.web_tool.DDGS") as client, patch("xuloom.tools.web_tool.SEARCH_RETRY_DELAY", 0), patch("xuloom.tools.native_search.execute") as native:
            client.return_value.text.side_effect = [SearchError("DNS resolution failed"), ROWS]
            result = await tool().execute("unchanged query", 15)
        self.assertTrue(result["ok"])
        self.assertEqual(result["metadata"]["attempts"], 2)
        self.assertEqual(result["metadata"]["retry_history"][0]["code"], "dns_error")
        self.assertEqual(client.return_value.text.call_count, 2)
        self.assertEqual(client.return_value.text.call_args_list[0], client.return_value.text.call_args_list[1])
        native.assert_not_called()

    async def test_unknown_ddgs_failure_is_retried_only_once_then_returns_all_diagnostics(self):
        with patch("xuloom.tools.web_tool.DDGS") as client, patch("xuloom.tools.web_tool.SEARCH_RETRY_DELAY", 0):
            client.return_value.text.side_effect = SearchError("temporary unknown service error private-model-key")
            result = await tool().execute("query")
        self.assertTrue(result["is_error"])
        self.assertEqual(client.return_value.text.call_count, 2)
        self.assertTrue(result["metadata"]["retryable"])
        self.assertTrue(result["metadata"]["automatic_retry_exhausted"])
        self.assertEqual(len(result["metadata"]["retry_history"]), 2)
        self.assertNotIn("private-model-key", json.dumps(result))

    async def test_access_tls_proxy_auth_and_empty_results_are_not_blindly_retried(self):
        for message in ["HTTP 403 Forbidden", "certificate verify failed", "HTTP 407 Proxy authentication required", "No results found."]:
            with self.subTest(message=message), patch("xuloom.tools.web_tool.DDGS") as client:
                client.return_value.text.side_effect = SearchError(message)
                result = await tool().execute("query")
                self.assertFalse(result["metadata"]["retryable"])
                self.assertEqual(client.return_value.text.call_count, 1)
        with patch("xuloom.tools.web_tool.DDGS") as client:
            client.return_value.text.return_value = []
            result = await tool().execute("query")
            self.assertEqual(result["metadata"]["code"], "no_results")
            self.assertEqual(client.return_value.text.call_count, 1)

    async def test_long_retry_after_returns_cooldown_instead_of_retrying_too_soon(self):
        for header in ["30", format_datetime(datetime.now(timezone.utc) + timedelta(seconds=60), usegmt=True)]:
            response = httpx.Response(429, headers={"Retry-After": header}, request=httpx.Request("GET", "https://search.invalid"))
            error = httpx.HTTPStatusError("limited", request=response.request, response=response)
            with patch("xuloom.tools.web_tool.DDGS") as client:
                client.return_value.text.side_effect = error
                result = await tool().execute("query")
                self.assertEqual(client.return_value.text.call_count, 1)
                self.assertEqual(result["metadata"]["code"], "rate_limited")
                self.assertGreaterEqual(result["metadata"]["retry_after_seconds"], 29)
                self.assertEqual(result["metadata"]["retry_skipped"], "cooldown_or_deadline")

    async def test_abort_during_backoff_never_starts_next_request(self):
        abort, entered = asyncio.Event(), asyncio.Event()
        loop = asyncio.get_running_loop()
        def fail(*args, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            raise SearchError("HTTP 429 Rate limited")
        with patch("xuloom.tools.web_tool.DDGS") as client, patch("xuloom.tools.web_tool.SEARCH_RETRY_DELAY", 2):
            client.return_value.text.side_effect = fail
            task = asyncio.create_task(tool(abort=abort).execute("query"))
            await entered.wait(); abort.set()
            with self.assertRaises(asyncio.CancelledError): await asyncio.wait_for(task, 0.5)
            self.assertEqual(client.return_value.text.call_count, 1)

    async def test_overall_deadline_does_not_retry_while_original_thread_is_running(self):
        release = threading.Event()
        def slow(*args, **kwargs):
            release.wait(2)
            return ROWS
        with patch("xuloom.tools.web_tool.DDGS") as client, patch("xuloom.tools.web_tool.SEARCH_TOTAL_TIMEOUT", 0.05):
            client.return_value.text.side_effect = slow
            try:
                result = await asyncio.wait_for(tool().execute("query"), 0.5)
                self.assertEqual(result["metadata"]["code"], "timeout")
                self.assertEqual(result["metadata"]["retry_skipped"], "overall_deadline")
                self.assertEqual(client.return_value.text.call_count, 1)
            finally:
                release.set()


@unittest.skipIf(sys.version_info < (3, 10), "Modern DDGS transport; legacy adapter is tested separately")
class ProxyRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import primp
        from ddgs.ddgs import DDGS as RealDDGS
        self.native, self.real = primp, RealDDGS
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.server.auth.append(self.headers.get("Authorization"))
                self.send_response(getattr(self.server,"status",200))
                if hasattr(self.server,"retry_after"): self.send_header("Retry-After",self.server.retry_after)
                self.end_headers(); self.wfile.write(self.server.label.encode())
            def log_message(self, *args): pass
        self.servers = []
        for label in ["origin", "proxy"]:
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server.label, server.auth = label, []
            threading.Thread(target=server.serve_forever, daemon=True).start(); self.servers.append(server)
            self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        self.origin = "http://127.0.0.1:" + str(self.servers[0].server_port)
        self.proxy = "http://127.0.0.1:" + str(self.servers[1].server_port)

    def engine(self):
        parent = self
        from ddgs.results import TextResult
        class LocalEngine:
            provider, name = "fixture", "fixture"
            def __init__(self): self.http_client = SimpleNamespace(client=parent.native.Client(timeout=2))
            def search(self, *args, **kwargs):
                response = self.http_client.client.request("GET", parent.origin)
                return [TextResult(href="https://evidence.invalid/paper", title=response.text, body="Evidence")]
        return LocalEngine()

    async def test_simultaneous_direct_and_system_use_separate_routes_without_environment_changes(self):
        proxies = {"http": self.proxy, "https": self.proxy}
        ambient = {k:v for k,v in os.environ.items() if k.upper() not in {"HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","NO_PROXY","DDGS_PROXY"}}
        ambient.update(HTTP_PROXY=self.proxy, HTTPS_PROXY=self.proxy, ALL_PROXY=self.proxy, DDGS_PROXY="invalid-ignored-proxy")
        with patch.dict(os.environ, ambient, clear=True), patch("xuloom.tools.web_network.getproxies", return_value=proxies), patch.object(self.real, "_get_engines", side_effect=lambda *a, **k: [self.engine()]):
            before = dict(os.environ)
            direct, system = await asyncio.gather(tool("direct").execute("direct"), tool("system").execute("system"))
            self.assertEqual(dict(os.environ), before)
        self.assertTrue(direct["ok"], direct); self.assertTrue(system["ok"], system)
        self.assertIn("Title: origin", direct["content"])
        self.assertIn("Title: proxy", system["content"])
        self.assertFalse(direct["metadata"]["proxy_used"])
        self.assertTrue(system["metadata"]["proxy_used"])
        self.assertTrue(all(value is None for server in self.servers for value in server.auth), "Model API keys never go to search/proxy endpoints")

    async def test_no_proxy_bypass_is_honored_even_with_system_proxy_configured(self):
        with patch("xuloom.tools.web_network.getproxies", return_value={"https": self.proxy, "http": self.proxy, "no": "*"}), patch.object(self.real, "_get_engines", side_effect=lambda *a, **k: [self.engine()]):
            result = await tool("system").execute("bypass")
        self.assertTrue(result["ok"], result)
        self.assertIn("Title: origin", result["content"])
        self.assertFalse(result["metadata"]["proxy_used"])

    async def test_real_upstream_http_status_and_cooldown_survive_ddgs_wrapping(self):
        self.servers[0].status, self.servers[0].retry_after = 503, "30"
        with patch.object(self.real, "_get_engines", side_effect=lambda *a, **k: [self.engine()]):
            result = await tool("direct").execute("temporary outage")
        self.assertTrue(result["is_error"])
        self.assertEqual(result["metadata"]["http_status"], 503)
        self.assertEqual(result["metadata"]["retry_after_seconds"], 30)
        self.assertEqual(result["metadata"]["attempts"], 1)
        self.assertIn("HTTPStatusError", result["metadata"]["detail"])
        self.assertIn("127.0.0.1", result["metadata"]["detail"])
