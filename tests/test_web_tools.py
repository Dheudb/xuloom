import asyncio
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from xuloom.tools.web_tool import WebSearchTool, WebFetchInput
from xuloom.tools.tool_search_tool import ToolSearchTool


class WebTests(unittest.IsolatedAsyncioTestCase):
    async def test_web_fetch_alias_is_canonical_before_authorization_without_conflicts(self):
        self.assertEqual(WebFetchInput(file_path="https://example.org/paper").model_dump(), {"url":"https://example.org/paper", "prompt":""})
        self.assertNotIn("file_path", WebFetchInput.model_json_schema()["properties"])
        for invalid in ({"file_path":"local.txt"}, {"url":"https://example.org/a", "file_path":"https://example.org/b"}):
            with self.assertRaises(ValidationError): WebFetchInput(**invalid)
        from xuloom.tools.file_read_tool import FileReadTool
        from xuloom.protocols.dsml import ToolProtocolError
        with self.assertRaisesRegex(ToolProtocolError, "web_fetch_tool"):
            FileReadTool().prepare_input({"file_path":"https://example.org/paper"})

    async def test_search_timeouts_and_access_denials_have_actionable_distinct_codes(self):
        import httpx
        from xuloom.tools.web_tool import network_error, SearchTimeout, SearchRateLimit
        for error, code in ((SearchTimeout("timeout"), "timeout"), (SearchRateLimit("rate limit"), "rate_limited")):
            result = network_error(error, {})
            self.assertEqual(result["code"], code); self.assertTrue(result["retryable"])
        response = httpx.Response(403, request=httpx.Request("GET", "https://example.org"))
        result = network_error(httpx.HTTPStatusError("Forbidden", request=response.request, response=response), {})
        self.assertEqual(result["code"], "access_denied"); self.assertFalse(result["retryable"])

    async def test_broad_search_requests_reach_provider_without_old_ten_result_cap(self):
        tool = WebSearchTool()
        tool.context = {}
        with patch("xuloom.tools.web_tool.DDGS") as client:
            client.return_value.text.return_value = [
                {"title": f"Paper {i}", "href": f"https://example.org/{i}", "body": "Evidence"}
                for i in range(60)]
            for limit in (15, 30, 50):
                with self.subTest(limit=limit):
                    result = await tool(query="broad literature review", max_results=limit)
                    self.assertTrue(result["ok"])
                    client.return_value.text.assert_called_with("broad literature review", max_results=limit)
                    self.assertEqual(result["metadata"]["result_count"], limit)
                    self.assertIn(f"URL: https://example.org/{limit - 1}\n", result["content"])
                    self.assertNotIn(f"URL: https://example.org/{limit}\n", result["content"])
            client.return_value.text.return_value = [{"title": "Only paper", "href": "https://example.org/one"}]
            result = await tool(query="narrow topic", max_results=50)
            self.assertEqual(result["metadata"]["result_count"], 1)
            await tool(query="ordinary search")
            client.return_value.text.assert_called_with("ordinary search", max_results=5)
            before = client.call_count
            for invalid in (0, 51):
                with self.assertRaises(ValidationError):
                    await tool(query="invalid", max_results=invalid)
            self.assertEqual(client.call_count, before, "Invalid input must not reach the network")

    async def test_search_results_and_provider_failures_are_distinct(self):
        with patch("xuloom.tools.web_tool.DDGS") as client:
            client.return_value.text.return_value = [{"title": "Paper", "href": "https://example.org/paper", "body": "Chemical reasoning"}, {"href": "javascript:invalid"}]
            result = await WebSearchTool().execute("chemistry reasoning", 3)
            self.assertTrue(result["ok"])
            self.assertIn("https://example.org/paper", result["content"])
            self.assertEqual(result["metadata"]["result_count"], 1)
            client.return_value.text.return_value = []
            result = await WebSearchTool().execute("chemistry reasoning")
            self.assertTrue(result["is_error"])
            self.assertEqual(result["metadata"]["reason"], "no_results")
            client.return_value.text.side_effect = RuntimeError("provider blocked")
            result = await WebSearchTool().execute("chemistry reasoning")
            self.assertTrue(result["is_error"])
            self.assertIn("not a missing tool", result["content"])
        self.assertEqual(WebFetchInput(url="https://example.org").prompt, "")

    async def test_stop_does_not_wait_for_synchronous_search(self):
        entered, release = threading.Event(), threading.Event()
        def slow_search(*args, **kwargs):
            entered.set()
            release.wait(3)
            return []
        with patch("xuloom.tools.web_tool.DDGS") as client:
            client.return_value.text.side_effect = slow_search
            task = asyncio.create_task(WebSearchTool().execute("query"))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 0.5)
            finally:
                release.set()

    async def test_natural_language_discovery_and_explicit_activation(self):
        from xuloom.tools.web_tool import WebFetchTool
        search, fetch = WebSearchTool(), WebFetchTool()
        tool = ToolSearchTool()
        active = {search.name: search}
        latent = {fetch.name: fetch}
        def activate(names):
            for name in names:
                active[name] = latent.pop(name)
            return names
        tool.context = {"engine_available_tools": active, "latent_tools_registry": latent, "activate_tools": activate}
        for query, expected in [("web search internet access search the web for information", "web_search_tool"), ("fetch URL http request browse page", "web_fetch_tool"), ("帮我联网搜索网页", "web_search_tool")]:
            result = await tool.execute(query)
            self.assertIn(expected, result["content"])
            self.assertGreater(result["metadata"]["match_count"], 0)
        self.assertEqual((await tool.execute("unmatchablexyz123"))["metadata"]["match_count"], 0)
        result = await tool.execute("fetch URL", activate=["web_fetch_tool"])
        self.assertEqual(result["metadata"]["activated"], ["web_fetch_tool"])

    async def test_engine_exposes_search_without_discovery_and_reports_real_status(self):
        from xuloom.core.engine import QueryEngine
        from xuloom.memory.memory import MemoryCompactor
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"OPENAI_API_KEY": "fixture", "XULOOM_MODEL_PROVIDER": "openai"}), patch.object(MemoryCompactor, "_resolve_token_encoder", return_value=None):
            engine = QueryEngine(model="fixture", model_provider="openai", cwd=temp)
            try:
                events = []
                engine.tool_context["capability_event"] = events.append
                self.assertIn("web_search_tool", engine.available_tools)
                self.assertIn("web_fetch_tool", engine.available_tools)
                self.assertNotIn("web_search_tool", engine.latent_tools)
                calls = [{"id": "search", "name": "web_search_tool", "input": {"query": "chemistry reasoning", "max_results": 15}}]
                with patch("xuloom.tools.web_tool.DDGS") as client:
                    client.return_value.text.return_value = [{"title": "Paper", "href": "https://example.org/paper", "body": "proof"}]
                    results = await engine._execute_tool_batches(calls, lambda text: None)
                    self.assertFalse(results[0]["is_error"])
                    client.return_value.text.assert_called_with("chemistry reasoning", max_results=15)
                    self.assertIn("https://example.org/paper", str(results[0]))
                    client.return_value.text.return_value = []
                    results = await engine._execute_tool_batches(calls, lambda text: None)
                    self.assertTrue(results[0]["is_error"])
                    self.assertTrue([event for event in events if event["type"] == "tool_result"][-1]["is_error"])
            finally:
                await engine.client.close()
                await engine.compactor.client.close()
                engine.session_manager.release_writer()
