"""Local regressions for worker settlement, stalled evidence and waiting traces."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

from xuloom.core.engine import QueryEngine
from xuloom.desktop.turns import TurnRecorder
from xuloom.memory.memory import MemoryCompactor


class WaitingTraceTests(unittest.TestCase):
    def test_bridge_checkpoints_after_dequeued_wait_event_not_before(self):
        from xuloom.desktop.bridge import DesktopBridge
        bridge = DesktopBridge.__new__(DesktopBridge)
        bridge.recorder = TurnRecorder()
        bridge.engine = NS(persist_session_state=Mock(return_value=True))
        bridge.output = Mock()
        DesktopBridge.emit(bridge, dict(type="subagents_waiting", count=1, message="Waiting"))
        bridge.engine.persist_session_state.assert_called_once()
        self.assertEqual(bridge.recorder.record["runtimeStatus"], "waiting_subagents")

    def test_request_completion_does_not_complete_task_or_clear_worker_wait(self):
        recorder = TurnRecorder()
        recorder.accept(dict(type="subagents_waiting", count=1, message="Waiting for evidence"))
        recorder.accept(dict(type="stream_status", turn=1, metrics={"status": "completed"}))
        self.assertEqual(recorder.record["runtimeStatus"], "waiting_subagents")
        self.assertEqual(recorder.record["outputChannel"], "commentary")
        self.assertEqual(recorder.record["answer"], "")
        self.assertEqual(recorder.record["status"], "running")
        recorder.accept(dict(type="subagents_waiting", count=0, message="Synthesizing"))
        recorder.accept(dict(type="stream_status", turn=2, metrics={"status": "completed"}))
        self.assertNotEqual(recorder.record["runtimeStatus"], "completed")

    def test_wait_marks_draft_as_provisional_and_keeps_final_separate(self):
        recorder = TurnRecorder()
        recorder.accept(dict(type="message_start", channel="commentary"))
        recorder.accept(dict(type="text_delta", channel="commentary", text="PRELIMINARY"))
        recorder.accept(dict(type="subagents_waiting", count=1, message="Waiting for evidence"))
        self.assertTrue(recorder.record["steps"][0]["provisional"])
        recorder.accept(dict(type="message_start"))
        recorder.accept(dict(type="final", text="VERIFIED"))
        recorder.accept(dict(type="turn_finished"))
        self.assertEqual(recorder.record["answer"], "VERIFIED")


class SettlementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"XULOOM_DATA_DIR": str(self.root / "data"),
            "XULOOM_CONFIG_DIR": str(self.root / "config"), "USERPROFILE": str(self.root / "home"),
            "HOME": str(self.root / "home"), "OPENAI_API_KEY": "fixture", "XULOOM_MODEL_PROVIDER": "openai"})
        self.env.start()
        self.tokens = patch.object(MemoryCompactor, "_resolve_token_encoder", return_value=None)
        self.tokens.start()
        self.engine = QueryEngine(model="fixture", model_provider="openai", cwd=str(self.root), agent_depth=1)

    async def asyncTearDown(self):
        await self.engine.aclose()
        self.engine.session_manager.release_writer()
        self.tokens.stop()
        self.env.stop()
        self.temp.cleanup()

    async def test_wait_checkpoint_is_saved_before_sleep_and_summary_requested_once(self):
        gate, waiting = asyncio.Event(), asyncio.Event()
        workers = self.engine.tool_context["subagent_tasks"]
        async def job():
            await gate.wait()
            return {"status": "completed", "result": "verified evidence"}
        workers.launch("worker", job())
        self.engine.subagent_registry.append(dict(agent_id="worker", status="running", backend="native", turn=1,
                                                  pending_messages=[], executor="local"))
        recorder = TurnRecorder()
        self.engine.turn_traces.append(recorder.record)
        saved = []
        def save():
            saved.append(json.loads(json.dumps(recorder.record)))
            return True
        async def emit(event):
            recorder.accept(event)
            if event["type"] == "subagents_waiting" and event["count"]:
                waiting.set()
        with patch.object(self.engine, "persist_session_state", side_effect=save):
            task = asyncio.create_task(self.engine._collect_subagents_before_final(turn=1, event_callback=emit))
            try:
                await asyncio.wait_for(waiting.wait(), 3)
                await asyncio.sleep(0)
                self.assertTrue(any(t.get("runtimeStatus") == "waiting_subagents" for t in saved))
                row = self.engine.subagent_registry[0]
                self.assertEqual(len(row["pending_messages"]), 1)
                self.assertEqual(row["pending_messages"][0]["kind"], "settle")
                from xuloom.agents.agent_tool import AgentTool
                tool = AgentTool()
                tool.context = self.engine.tool_context
                await tool._request_worker_summaries(["worker"])
                self.assertEqual(len(row["pending_messages"]), 1, "No repeated nudges or extra model calls")
            finally:
                gate.set()
                await asyncio.wait_for(task, 3)

    async def test_readonly_worker_stalls_are_blocked_but_new_pages_continue(self):
        from xuloom.agents.progress import observe
        self.engine.tool_context["delegation"] = {"role_definition": {"read_only": True}}
        state = {}
        def batch(identity, digest, name="web_fetch_tool"):
            self.engine.recent_tool_activity.insert(0, {"tool_use_id": identity, "tool_name": name,
                "observation_digest": digest, "status": "ok"})
            return [{"tool_use_id": identity, "is_error": False}]
        for n in range(20):
            self.assertIsNone(await observe(self.engine, batch(str(n), str(n)), state, turn=n))
        for n in range(3):
            self.assertIsNone(await observe(self.engine, batch(f"repeat{n}", "19"), state, turn=20+n))
        self.assertTrue(any(m.get("_origin") == "worker_stall" for m in self.engine.messages))
        result = None
        for n in range(2):
            result = await observe(self.engine, batch(f"still{n}", "19"), state, turn=23+n)
        self.assertEqual(result.reason, "blocked")
        self.assertFalse(result.ok)
        self.assertIn("Repeated read-only", result.text)
        self.assertEqual(self.engine.last_run_result["reason"], "blocked")
        state = {}
        for n in range(10):
            self.assertIsNone(await observe(self.engine, batch(f"write{n}", "same", "file_write_tool"), state, turn=n))
        self.assertNotIn("worker_stall_count", state, "Do not judge progress of mutations or arbitrary tools")

    async def test_evidence_digest_ignores_archive_path_and_preserves_changed_content(self):
        from xuloom.tools.web_tool import WebFetchTool
        fetch = self.engine.available_tools["web_fetch_tool"]
        fetch.execute = AsyncMock(side_effect=[
            {"ok": True, "is_error": False, "metadata": {"url": "https://example.invalid/a"}, "content": "A" * 6000},
            {"ok": True, "is_error": False, "metadata": {"url": "https://example.invalid/a"}, "content": "A" * 6000},
            {"ok": True, "is_error": False, "metadata": {"url": "https://example.invalid/a"}, "content": "B" * 6000}])
        self.assertIsInstance(fetch, WebFetchTool)
        for n in range(3):
            await self.engine._execute_tool_batches([{"id": str(n), "name": fetch.name,
                "input": {"url": "https://example.invalid/a"}}], lambda _: None)
        rows = list(reversed(self.engine.recent_tool_activity[:3]))
        self.assertEqual(rows[0]["observation_digest"], rows[1]["observation_digest"])
        self.assertNotEqual(rows[1]["observation_digest"], rows[2]["observation_digest"])

    async def test_production_worker_loop_stops_repeated_fetches_with_blocked_receipt(self):
        self.engine.tool_context["delegation"] = {"role_definition": {"read_only": True}}
        calls = []
        async def sample(**kwargs):
            calls.append(kwargs)
            return NS(content=[NS(type="tool_use", id=str(len(calls)), name="web_fetch_tool",
                input={"url": "https://example.invalid/same"})], stop_reason="tool_use", usage=None), [], False
        self.engine._stream_model_response = sample
        fetch = self.engine.available_tools["web_fetch_tool"]
        fetch.execute = AsyncMock(return_value={"ok": True, "is_error": False, "metadata": {}, "content": "Identical evidence"})
        result = await self.engine.run("Investigate the assigned source", sys_print_callback=lambda _: None)
        self.assertEqual(result.reason, "blocked")
        self.assertFalse(result.ok)
        self.assertEqual(len(calls), 6)
        self.assertIn("Worker progress check", str(calls[-1]["api_messages"]))
        self.assertEqual(len(self.engine.recent_tool_activity), 6)

    async def test_ready_worker_satisfies_settlement_without_an_extra_model_call(self):
        pending, calls = [], []
        self.engine.tool_context["delegation"] = {"role_definition": {"read_only": True}}
        self.engine.tool_context["delegated_mailbox"] = lambda: list(pending)
        def acknowledge(identities):
            pending[:] = [m for m in pending if m["id"] not in identities]
        self.engine.tool_context["ack_delegated_mailbox"] = acknowledge
        async def sample(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                pending.append(dict(id="settle", kind="settle", text="Return the current result"))
            return NS(content=[NS(type="text", text="COMPLETE")], stop_reason="end_turn", usage=None), [], False
        self.engine._stream_model_response = sample
        result = await self.engine.run("Return findings", sys_print_callback=lambda _: None)
        self.assertTrue(result.ok, result)
        self.assertEqual(len(calls), 1, "A ready final result already satisfies settlement")
        self.assertEqual(pending, [])


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    def test_apple_documentation_json_is_readable_instead_of_raw_navigation_data(self):
        from xuloom.tools.web_tool import extract_page
        payload = {"schemaVersion": {"major": 0, "minor": 3}, "metadata": {"title": "Universal Links"},
            "primaryContentSections": [{"kind": "content", "content": [
                {"type": "paragraph", "inlineContent": [{"type": "text", "text": "Verify domain ownership. "},
                    {"type": "codeVoice", "code": "apple-app-site-association"},
                    {"type": "reference", "identifier": "doc://manual"}]},
                {"type": "codeListing", "code": ["first line", "second line"]}]}],
            "references": {"doc://manual": {"title": "Association file", "url": "/documentation/association"},
                           "unused": {"title": "NOT_PART_OF_ARTICLE", "abstract": "Navigation " * 1000}}}
        text, title = extract_page(json.dumps(payload), False, "https://developer.apple.com/tutorials/data/documentation/example.json")
        self.assertEqual(title, "Universal Links")
        self.assertIn("Verify domain ownership.", text)
        self.assertIn("apple-app-site-association", text)
        self.assertIn("Association file", text)
        self.assertIn("https://developer.apple.com/documentation/association", text)
        self.assertIn("first line\nsecond line", text)
        self.assertNotIn("NOT_PART_OF_ARTICLE", text)
        for original in ['{"arbitrary":"unchanged JSON"}', 'not JSON']:
            self.assertEqual(extract_page(original, False), (original, ""))

    async def test_fetch_respects_connection_route_even_when_ambient_proxy_exists(self):
        import httpx
        from xuloom.tools.web_tool import WebFetchTool
        real_client = httpx.AsyncClient
        routes = []
        def client(**kwargs):
            routes.append(kwargs.get("trust_env"))
            return real_client(**kwargs, transport=httpx.MockTransport(lambda request:
                httpx.Response(200, text="<html><body>Verified</body></html>", headers={"content-type": "text/html"})))
        for mode, expected in [("direct", False), ("system", True)]:
            tool = WebFetchTool()
            tool.context = {"native_search_options": lambda mode=mode: {"network_mode": mode, "profile": {"backend": "builtin"}}}
            with patch("httpx.AsyncClient", side_effect=client):
                result = await tool.execute("https://example.invalid/doc")
            self.assertTrue(result["ok"], result)
            self.assertEqual(routes[-1], expected)
            self.assertEqual(result["metadata"]["network_mode"], mode)
