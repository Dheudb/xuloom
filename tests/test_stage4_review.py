"""Regression tests for review findings on the cost cap, strict replay and the HTTP server."""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xuloom import ApprovalPolicy, EngineConfig, QueryEngine
from xuloom.models import model_response
from xuloom.server import XuloomServer
from xuloom.testing import ScriptedModel
from xuloom.utils.vcr import ReplayMissError

PRICES = {"m": {"input": 1000, "output": 1000}}  # $0.001 per token keeps the numbers readable


class Isolated(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "work").mkdir()
        self.env = patch.dict(os.environ, {"XULOOM_DATA_DIR": str(self.root / "data"), "HOME": str(self.root / "home"),
                                           "USERPROFILE": str(self.root / "home")})
        self.env.start()
        os.environ.pop("XULOOM_VCR_MODE", None)
        self.model = ScriptedModel(default=model_response(text="summary of earlier work", input_tokens=100, output_tokens=50))
        self.model.install()
        self.config = self.model.config(EngineConfig(model="m", cwd=str(self.root / "work"), collaboration_mode="solo",
                                                     approval_policy=ApprovalPolicy("full"), model_prices=PRICES))

    async def asyncTearDown(self):
        self.model.uninstall()
        self.env.stop()
        self.temp.cleanup()

    async def history(self, engine, turns=4):
        for index in range(turns):
            self.model.script.append(model_response(text=f"answer {index} " + "x" * 400, input_tokens=10, output_tokens=10))
            await engine.run(f"question {index} " + "y" * 400, sys_print_callback=lambda *_: None)


class CompactionCostTests(Isolated):
    async def test_compaction_requests_respect_the_cap(self):
        async with QueryEngine(self.config) as engine:
            await self.history(engine)
            engine.cost_meter.start_task()
            engine.cost_meter.limit_usd = 0.0
            before = len(self.model.requests)
            with self.assertRaises(Exception) as caught:
                await engine._apply_layered_compaction_if_needed(lambda *_: None, force=True)
        chain, error = [], caught.exception
        while error is not None:
            chain.append(type(error).__name__)
            error = error.__cause__ or error.__context__
        self.assertIn("CostLimitError", chain)
        self.assertEqual(len(self.model.requests), before)  # nothing was sent

    async def test_compaction_is_priced_into_the_task(self):
        async with QueryEngine(self.config) as engine:
            await self.history(engine)
            engine.cost_meter.start_task()
            before = len(self.model.requests)
            try:
                await engine._apply_layered_compaction_if_needed(lambda *_: None, force=True)
            except Exception:
                pass  # the short fixture history may not shrink; the summary request still happened
            self.assertGreater(len(self.model.requests), before)
            spent = engine.cost_meter.task_usd
            self.assertGreater(spent, 0)
            self.assertAlmostEqual(spent, engine.auxiliary_usage["compaction"]["cost_usd"])

    async def test_run_stops_before_compacting_when_the_cap_is_reached(self):
        async with QueryEngine(self.config.replace(max_cost_usd=0.0)) as engine:
            with patch.object(type(engine), "_apply_layered_compaction_if_needed") as compaction:
                result = await engine.run("hi", sys_print_callback=lambda *_: None)
        self.assertEqual(result.reason, "cost_limit")
        compaction.assert_not_called()
        self.assertEqual(self.model.requests, [])


class ResumeCostTests(Isolated):
    async def test_resuming_a_stopped_task_keeps_its_spend(self):
        from xuloom.models import ToolCall
        self.model.default = None
        # One $2 request that asks for a tool; the next request would exceed the $1 cap.
        self.model.script = [model_response(tool_calls=[ToolCall("c1", "file_write_tool", {"file_path": "a.txt", "content": "x"})],
                                            input_tokens=2000)]
        config = self.config.replace(max_cost_usd=1.0)
        async with QueryEngine(config) as engine:
            result = await engine.run("write", sys_print_callback=lambda *_: None)
            self.assertEqual(result.reason, "cost_limit")
            session_id = engine.session_id
        requests = len(self.model.requests)
        self.model.script = [model_response(text="should not be requested")]
        async with QueryEngine(config) as engine:
            self.assertTrue(engine.load_session(session_id))
            self.assertAlmostEqual(engine.cost_meter.task_usd, 2.0)
            events = [event async for event in engine.submit_message("", sys_print_callback=lambda *_: None, resume=True)]
            self.assertEqual(events[-1]["result"]["reason"], "cost_limit")
            # A new task (not a resume) starts with a fresh task total.
            self.model.script = [model_response(text="new task", input_tokens=10)]
            fresh = await engine.run("something else", sys_print_callback=lambda *_: None)
            self.assertTrue(fresh.ok, fresh)
        self.assertEqual(len(self.model.requests), requests + 1)


class StrictReplayTests(Isolated):
    async def test_saved_session_cannot_turn_strict_replay_off(self):
        self.model.default = None
        self.model.script = [model_response(text="recorded live")]
        async with QueryEngine(self.config) as engine:
            await engine.run("hi", sys_print_callback=lambda *_: None)
            session_id = engine.session_id
        self.assertEqual(len(self.model.requests), 1)
        with patch.dict(os.environ, {"XULOOM_VCR_MODE": "replay"}):
            async with QueryEngine(self.config) as engine:
                self.assertTrue(engine.load_session(session_id))
                self.assertEqual(engine.vcr_manager.mode, "replay")
                result = await engine.run("not recorded", sys_print_callback=lambda *_: None)
        self.assertFalse(result.ok)
        self.assertEqual(len(self.model.requests), 1)  # the adapter was not called again

    async def test_session_mode_still_applies_without_an_explicit_setting(self):
        from xuloom.utils.vcr import VCRManager
        manager = VCRManager(cassette_dir=str(self.root))
        manager.load_state({"mode": "record"})
        self.assertEqual(manager.mode, "record")
        explicit = VCRManager(cassette_dir=str(self.root), mode="replay")
        explicit.load_state({"mode": "off"})
        self.assertEqual(explicit.mode, "replay")
        with self.assertRaises(ReplayMissError):
            explicit.try_replay({"x": 1})


class StrictReplayAuxiliaryTests(Isolated):
    async def test_compaction_never_reaches_the_model_in_strict_replay(self):
        async with QueryEngine(self.config) as engine:
            await self.history(engine)
            before = len(self.model.requests)
            engine.vcr_manager.mode = "replay"
            with self.assertRaises(Exception) as caught:
                await engine._apply_layered_compaction_if_needed(lambda *_: None, force=True)
        chain, error = [], caught.exception
        while error is not None:
            chain.append(type(error).__name__)
            error = error.__cause__ or error.__context__
        self.assertIn("ReplayMissError", chain)
        self.assertEqual(len(self.model.requests), before)

    async def test_memory_requests_are_refused_in_strict_replay(self):
        from xuloom.core.config import safe_llm_call
        async with QueryEngine(self.config) as engine:
            engine.vcr_manager.mode = "replay"
            with self.assertRaises(ReplayMissError):
                await safe_llm_call(engine.compactor.client, "m", self.model.name, [{"role": "user", "content": "x"}], 100,
                                    usage_callback=engine.compactor.usage_callback, purpose="session_memory",
                                    guard=engine.compactor.request_guard)
        self.assertEqual(self.model.requests, [])


class ServerDisconnectTests(Isolated):
    async def test_closing_the_stream_while_the_model_waits_stops_the_task(self):
        gate = asyncio.Event()

        async def slow(request):
            await gate.wait()
            return model_response(text="too late")
        self.model.default = None
        self.model.script = [slow]
        server = XuloomServer(self.config, root=self.root / "work", token="disconnect-token-123", port=0)
        host, port = await server.start()
        try:
            import httpx
            client = httpx.AsyncClient(base_url=f"http://{host}:{port}/v1", headers={"Authorization": "Bearer disconnect-token-123"},
                                       trust_env=False, timeout=30)
            sid = (await client.post("/sessions", json={})).json()["session_id"]
            await client.aclose()
            reader, writer = await asyncio.open_connection(host, port)
            body = b'{"text": "wait", "stream": true}'
            writer.write(b"POST /v1/sessions/" + sid.encode() + b"/messages HTTP/1.1\r\nHost: x\r\n"
                         b"Authorization: Bearer disconnect-token-123\r\nContent-Type: application/json\r\n"
                         b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
            session = server.sessions[sid]
            for _ in range(200):
                if self.model.requests:
                    break
                await asyncio.sleep(0.02)
            self.assertTrue(session.running)
            writer.transport.abort()  # client disappears while no event is being written
            for _ in range(250):
                if not session.running:
                    break
                await asyncio.sleep(0.02)
            self.assertFalse(session.running, "task kept running after the client disconnected")
            self.assertFalse(gate.is_set())
        finally:
            gate.set()
            await server.close()

    async def test_reset_while_sending_the_response_head_stops_the_task(self):
        from xuloom.server.app import _Request
        gate = asyncio.Event()

        async def slow(request):
            await gate.wait()
            return model_response(text="too late")
        self.model.default = None
        self.model.script = [slow]
        server = XuloomServer(self.config, root=self.root / "work", token="disconnect-token-123", port=0)
        try:
            sid = (await server._create_session({}))["session_id"]
            session = server.sessions[sid]

            class ResetWriter:
                def write(self, data):
                    pass

                async def drain(self):
                    raise ConnectionResetError("reset while sending headers")
            request = _Request("POST", f"/v1/sessions/{sid}/messages", {}, b'{"text": "wait", "stream": true}')
            with self.assertRaises(ConnectionResetError):
                await server._message(session, request, ResetWriter(), asyncio.StreamReader())
            for _ in range(250):
                if not session.running:
                    break
                await asyncio.sleep(0.02)
            self.assertFalse(session.running, "task kept running after the response head could not be sent")
            self.assertFalse(gate.is_set())
        finally:
            gate.set()
            await server.close()


if __name__ == "__main__":
    unittest.main()
