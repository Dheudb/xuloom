import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xuloom import EngineConfig, QueryEngine
from xuloom.core.config import safe_llm_call
from xuloom.core.network import create_model_client
from xuloom.models import (ModelAdapter, ToolCall, adapter_names, get_adapter, model_response, register_adapter,
                           to_openai_tools, unregister_adapter)


class ScriptedAdapter(ModelAdapter):
    """Plays back responses; records every request it receives."""
    name = "scripted-test"
    default_base_url = "http://127.0.0.1:9"

    def __init__(self, script=()):
        self.script = list(script)
        self.requests = []

    async def stream_turn(self, request):
        self.requests.append(request)
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        if callable(step):
            return await step(request)
        return step


class AdapterFixture(unittest.IsolatedAsyncioTestCase):
    """Isolated engine driven by a ScriptedAdapter (shared with other test modules)."""
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "work").mkdir()
        self.env = patch.dict(os.environ, {"XULOOM_DATA_DIR": str(self.root / "data"), "HOME": str(self.root / "home"),
                                           "USERPROFILE": str(self.root / "home")})
        self.env.start()
        self.adapter = ScriptedAdapter()
        register_adapter(self.adapter, replace=True)

    async def asyncTearDown(self):
        unregister_adapter(self.adapter.name)
        self.env.stop()
        self.temp.cleanup()

    def engine(self, **changes):
        return QueryEngine(EngineConfig(model="m", model_provider=self.adapter.name, api_key="k", cwd=str(self.root / "work"),
                                        network_mode="direct", collaboration_mode="solo",
                                        approval_policy=__import__("xuloom").ApprovalPolicy("full")).replace(**changes))


class ScriptedAdapterTests(AdapterFixture):

    async def test_tool_call_then_answer_runs_through_normal_tool_pipeline(self):
        self.adapter.script = [
            model_response(tool_calls=[ToolCall("c1", "file_write_tool", {"file_path": "a.txt", "content": "hi\n"})],
                           input_tokens=10, output_tokens=2),
            model_response(text="written", input_tokens=12, output_tokens=1),
        ]
        events = []
        async with self.engine() as engine:
            engine.set_event_handler(lambda event: None)
            async for event in engine.submit_message("write a.txt", sys_print_callback=lambda _: None):
                events.append(event)
            result = events[-1]["result"]
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["text"], "written")
            self.assertEqual((self.root / "work" / "a.txt").read_text(), "hi\n")
            self.assertEqual(engine.session_token_usage["input_tokens"], 22)
        second = self.adapter.requests[1]
        results = [block for message in second.messages for block in message.get("content", [])
                   if isinstance(block, dict) and block.get("type") == "tool_result"]
        self.assertEqual(results[0]["tool_use_id"], "c1")
        self.assertIn("file_write_tool", {tool["name"] for tool in second.tools})
        self.assertTrue(second.system_prompt)
        types = [event["type"] for event in events]
        self.assertIn("tool_scheduled", types)
        self.assertEqual(sum(1 for event in events if event["type"] == "text_delta"), 1)

    async def test_streamed_text_is_not_duplicated(self):
        async def streaming(request):
            await request.emit_status("receiving")
            await request.emit_text("he")
            await request.emit_text("llo")
            return model_response(text="hello")
        self.adapter.script = [streaming]
        async with self.engine() as engine:
            events = [event async for event in engine.submit_message("hi", sys_print_callback=lambda _: None)]
        self.assertEqual([event["text"] for event in events if event["type"] == "text_delta"], ["he", "llo"])

    async def test_transient_error_uses_fallback_model(self):
        class Busy(Exception):
            status_code = 503
        self.adapter.script = [Busy("busy"), model_response(text="from fallback")]
        async with self.engine(fallback_model="backup") as engine:
            result = await engine.run("hi", sys_print_callback=lambda _: None)
        self.assertEqual(result.text, "from fallback")
        self.assertEqual([request.model for request in self.adapter.requests], ["m", "backup"])

    async def test_invalid_response_fails_without_crashing_the_session(self):
        self.adapter.script = [object()]
        async with self.engine() as engine:
            result = await engine.run("hi", sys_print_callback=lambda _: None)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "api_error")

    async def test_abort_cancels_a_pending_adapter_call(self):
        started = asyncio.Event()

        async def slow(request):
            started.set()
            await asyncio.sleep(30)
        self.adapter.script = [slow]
        async with self.engine() as engine:
            run = asyncio.create_task(engine.run("hi", sys_print_callback=lambda _: None))
            await asyncio.wait_for(started.wait(), 5)
            await engine.request_abort()
            result = await asyncio.wait_for(run, 5)
        self.assertEqual(result.reason, "cancelled")

    async def test_outer_cancellation_also_cancels_the_adapter_call(self):
        started, finished = asyncio.Event(), []

        async def slow(request):
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                finished.append("cancelled")
                raise
        self.adapter.script = [slow]
        async with self.engine() as engine:
            run = asyncio.create_task(engine.run("hi", sys_print_callback=lambda _: None))
            await asyncio.wait_for(started.wait(), 5)
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
            # No orphaned adapter task survives the cancelled run.
            self.assertEqual(finished, ["cancelled"])
            others = [task for task in asyncio.all_tasks() if task is not asyncio.current_task() and not task.done()
                      and "stream_turn" in repr(task.get_coro())]
            self.assertEqual(others, [])

    async def test_each_engine_keeps_the_adapter_that_created_its_client(self):
        class Tagged(ScriptedAdapter):
            def create_client(self, **options):
                client = super().create_client(**options)
                client.created_by = self
                return client

            async def stream_turn(self, request):
                self.requests.append(request)
                return model_response(text="same" if request.client.created_by is self else "MISMATCH")
        first_adapter = Tagged()
        register_adapter(first_adapter, replace=True)
        async with self.engine() as first:
            second_adapter = Tagged()
            register_adapter(second_adapter, replace=True)
            async with self.engine() as second:
                self.assertEqual((await first.run("a", sys_print_callback=lambda _: None)).text, "same")
                self.assertEqual((await second.run("b", sys_print_callback=lambda _: None)).text, "same")
        self.assertEqual((len(first_adapter.requests), len(second_adapter.requests)), (1, 1))

    async def test_clients_without_weak_references_are_released_on_close(self):
        from xuloom.models import registry

        class SlotClient:
            __slots__ = ("closed",)

            def __init__(self):
                self.closed = False

            @property
            def is_closed(self):
                return self.closed

            async def close(self):
                self.closed = True

        class SlotAdapter(ScriptedAdapter):
            def create_client(self, **options):
                return SlotClient()
        register_adapter(SlotAdapter(), replace=True)
        before = len(registry._CLIENT_ADAPTERS_BY_ID)
        clients = []
        for _ in range(3):
            async with self.engine() as engine:
                clients += [engine.client, engine.compactor.client]
                self.assertEqual(len(registry._CLIENT_ADAPTERS_BY_ID), before + 2)  # main + compactor
        self.assertTrue(all(client.closed for client in clients))
        self.assertEqual(len(registry._CLIENT_ADAPTERS_BY_ID), before)

    async def test_summaries_use_adapter_complete_text(self):
        self.adapter.script = [model_response(text="summary", input_tokens=5, output_tokens=3)]
        usage = []
        client = create_model_client(provider=self.adapter.name, model="m", api_base_url="", api_key="k", network_mode="direct")
        try:
            text = await safe_llm_call(client, "m", self.adapter.name, [{"role": "user", "content": "x"}], 100,
                                       usage_callback=lambda purpose, value: usage.append(value))
        finally:
            await client.close()
        self.assertEqual(text, "summary")
        self.assertEqual(usage, [{"input_tokens": 5, "output_tokens": 3}])
        self.assertTrue(client.is_closed)


class RegistryTests(unittest.TestCase):
    def test_builtins_are_present_and_protected(self):
        self.assertTrue({"openai", "anthropic"} <= set(adapter_names()))
        self.assertTrue(get_adapter("openai").native)
        with self.assertRaises(ValueError):
            unregister_adapter("openai")
        impostor = ScriptedAdapter()
        impostor.name = "openai"
        with self.assertRaises(ValueError):
            register_adapter(impostor, replace=True)

    def test_validation(self):
        with self.assertRaises(TypeError):
            register_adapter(object())
        bad = ScriptedAdapter()
        bad.name = "Bad Name"
        with self.assertRaises(ValueError):
            register_adapter(bad)
        with self.assertRaises(ValueError):
            model_response(text="x", tool_calls=[ToolCall("1", "t")], stop_reason="end_turn")
        with self.assertRaises(ValueError):
            model_response(stop_reason="weird")

    def test_unknown_provider_is_rejected(self):
        with self.assertRaises(ValueError):
            create_model_client(provider="not-registered", model="m", api_base_url="", api_key="k", network_mode="direct")

    def test_openai_tool_conversion(self):
        self.assertEqual(to_openai_tools([{"name": "t", "description": "d", "input_schema": {"type": "object"}}]),
                         [{"type": "function", "function": {"name": "t", "description": "d", "parameters": {"type": "object"}}}])


if __name__ == "__main__":
    unittest.main()
