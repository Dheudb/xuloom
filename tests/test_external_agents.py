"""Real loopback HTTP/MCP, plus opt-in installed CLI contracts; no supplier quota."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx
from xuloom.agents.external.runtime import restore
from xuloom.agents.external.host import LocalHost, SOURCE_ID
from xuloom.agents.external.program import environment, resolve
from xuloom.core.engine import QueryEngine
from xuloom.core.messages import create_user_message
from xuloom.core.storage import IOPath
from xuloom.memory.memory import MemoryCompactor

MODEL = "claude-sonnet-4-5-20250929"
MCP = importlib.util.find_spec("mcp") is not None
PROGRAM = os.environ.get("XULOOM_TEST_CLAUDE_PROGRAM")


class ExternalFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xuloom-external-contract-")
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"HOME": str(self.root), "USERPROFILE": str(self.root),
            "XULOOM_CONFIG_DIR": str(self.root / "config"), "XULOOM_DATA_DIR": str(self.root / "data"),
            "XULOOM_SKILLS_PATHS": "",
            "HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": "", "NO_PROXY": "*"})
        self.env.start()
        self.config = patch("xuloom.core.config.CONFIG_FILE", self.root / "config.json"); self.config.start()
        self.encoder = patch.object(MemoryCompactor, "_resolve_token_encoder", return_value=None); self.encoder.start()
        self.records, self.engines, self.mode = [], [], "normal"
        self.release = threading.Event()
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fixture.records.append({"body": body, "headers": {k.lower(): v for k, v in self.headers.items()}})
                custom = getattr(fixture, "parent_handler", None)
                if custom is not None and custom(self, body): return
                if fixture.mode == "hold": fixture.release.wait(20)
                if self.path.endswith("/chat/completions"):
                    finished = any(message.get("role") == "tool" for message in body["messages"])
                    name = "Bash" if fixture.mode == "builtin" else "mcp__xuloom__file_read_tool"
                    def chunk(delta, reason=None):
                        return {"id": "chatcmpl_fixture", "object": "chat.completion.chunk", "created": 0, "model": body["model"],
                            "choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}
                    if finished:
                        events = [chunk({"role": "assistant", "content": "CONTROLLED_RESULT"}), chunk({}, "stop")]
                    else:
                        arguments = json.dumps({"file_path": "evidence.png" if fixture.mode == "image" else "evidence.txt"})
                        events = [chunk({"role": "assistant", "tool_calls": [{"index": 0, "id": "call_fixture", "type": "function", "function": {"name": name, "arguments": arguments[:12]}}]}),
                            chunk({"tool_calls": [{"index": 0, "function": {"arguments": arguments[12:]}}]}), chunk({}, "tool_calls")]
                    events.append({"id": "chatcmpl_fixture", "object": "chat.completion.chunk", "created": 0, "model": body["model"], "choices": [],
                        "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}})
                    if fixture.mode == "no_usage": events.pop()
                    if not body.get("stream"):
                        message = {"role": "assistant", "content": "CONTROLLED_RESULT"} if finished else {"role": "assistant", "content": None,
                            "tool_calls": [{"id": "call_fixture", "type": "function", "function": {"name": name, "arguments": json.dumps({"file_path": "evidence.txt"})}}]}
                        raw = json.dumps({"id": "chatcmpl_fixture", "object": "chat.completion", "model": body["model"], "created": 0,
                            "choices": [{"index": 0, "message": message, "finish_reason": "stop" if finished else "tool_calls"}],
                            "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}}).encode()
                    else: raw = ("".join("data: " + json.dumps(event) + "\n\n" for event in events) + ("" if fixture.mode == "truncated" else "data: [DONE]\n\n")).encode()
                    try:
                        self.send_response(200); self.send_header("Content-Type", "text/event-stream" if body.get("stream") else "application/json"); self.send_header("Content-Length", str(len(raw)))
                        self.end_headers(); self.wfile.write(raw)
                    except OSError: pass
                    return
                finished = any(isinstance(m.get("content"), list) and any(b.get("type") == "tool_result" for b in m["content"]) for m in body["messages"])
                writing = fixture.mode == "hold_after_write"
                if fixture.mode in {"hold_after_tool", "hold_after_write"} and finished: fixture.release.wait(20)
                block = {"type": "text", "text": ""} if finished else {"type": "tool_use", "id": "toolu_contract", "name": "mcp__xuloom__file_read_tool", "input": {}}
                if writing and not finished: block["name"] = "mcp__xuloom__file_write_tool"
                if fixture.mode == "builtin": block["name"] = "Bash"
                delta = {"type": "text_delta", "text": "CONTROLLED_RESULT"} if finished else {"type": "input_json_delta", "partial_json": json.dumps({"file_path": "evidence.png" if fixture.mode == "image" else "evidence.txt"})}
                if writing and not finished: delta["partial_json"] = json.dumps({"file_path": "created-once.txt", "content": "CREATED_ONCE"})
                start = {"id": "msg_contract", "type": "message", "role": "assistant", "model": MODEL, "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 12, "output_tokens": 0}}
                events = [{"type": "message_start", "message": start}, {"type": "content_block_start", "index": 0, "content_block": block},
                    {"type": "content_block_delta", "index": 0, "delta": delta}, {"type": "content_block_stop", "index": 0},
                    {"type": "message_delta", "delta": {"stop_reason": "end_turn" if finished else "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 5}}, {"type": "message_stop"}]
                if fixture.mode == "truncated": events.pop()
                if fixture.mode == "backpressure":
                    events = events[:1] + [{"type": "ping", "padding": "x" * 1000}] * 200 + events[1:]
                raw = "".join("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n" for e in events).encode()
                try:
                    self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.send_header("Content-Length", str(len(raw)))
                    self.end_headers(); self.wfile.write(raw)
                except OSError: pass  # Owner cancellation closes this loopback fixture socket.
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)
        (self.root / "evidence.txt").write_text("OWNED_EVIDENCE", encoding="utf8")

    def engine(self, provider="anthropic", **options):
        engine = QueryEngine(model=MODEL, model_provider=provider, api_key="fixture-only", api_base_url=self.base,
            cwd=self.root, network_mode="direct", collaboration_mode="ultra", **options)
        engine.permission_manager.approval_policy.set_mode("full")
        self.engines.append(engine)
        return engine

    async def asyncTearDown(self):
        self.release.set()
        await asyncio.gather(*(e.aclose() for e in self.engines))
        await asyncio.to_thread(self.server.shutdown); self.server.server_close(); self.thread.join(3)
        self.encoder.stop(); self.config.stop(); self.env.stop()
        # The fixture root is explicitly checked before recursive removal; CLI paths can exceed MAX_PATH.
        assert self.root.parent.resolve() == Path(tempfile.gettempdir()).resolve() and self.root.name.startswith("xuloom-external-contract-")
        def writable_remove(operation, path, exc_info):
            if not isinstance(exc_info[1], PermissionError): raise exc_info[1]
            assert IOPath(path).resolve().is_relative_to(IOPath(self.root).resolve())
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            operation(path)
        shutil.rmtree(IOPath(self.root), onerror=writable_remove); self.temp.cleanup()

    def state(self, engine):
        engine.require_writer(); engine.messages.append(create_user_message("Read evidence.txt"))
        engine.available_tools = {"file_read_tool": engine.available_tools["file_read_tool"]}
        state = {"seen_tool_ids": []}
        engine.tool_context["external_agent"] = state
        return state

    def request(self):
        return {"model": MODEL, "stream": True, "max_tokens": 100,
            "messages": [{"role": "user", "content": "Read evidence.txt"}],
            "tools": [{"name": "mcp__xuloom__file_read_tool", "input_schema": {"type": "object"}}]}


@unittest.skipUnless(MCP, "optional MCP runtime not installed")
class OpenAIHostTests(ExternalFixture):
    def request(self):
        return {"model": MODEL, "stream": True, "max_tokens": 100,
            "messages": [{"role": "user", "content": "Read evidence.txt"}],
            "tools": [{"type": "function", "function": {"name": "mcp__xuloom__file_read_tool", "parameters": {"type": "object"}}}]}

    async def test_fragmented_and_nonstream_tool_arguments_get_owned_identity(self):
        engine = self.engine("openai"); state = self.state(engine)
        async with httpx.AsyncClient(trust_env=False) as http:
            for streaming in (True, False):
                state["seen_tool_ids"] = []
                async with LocalHost(engine, state) as host:
                    result = await http.post(host.url + "/v1/chat/completions", json={**self.request(), "stream": streaming}, headers={"Authorization": "Bearer " + host.token})
                    self.assertEqual(result.status_code, 200)
                    self.assertIn(SOURCE_ID, result.text)
                    self.assertEqual(host.calls["call_fixture"], ("file_read_tool", {"file_path": "evidence.txt"}))
                    self.assertEqual(host.usage["output_tokens"], 5)
        self.assertTrue(all(r["headers"]["authorization"] == "Bearer fixture-only" for r in self.records))

    async def test_eof_before_done_never_grants_a_tool_identity(self):
        engine = self.engine("openai"); state = self.state(engine); self.mode = "truncated"
        async with LocalHost(engine, state) as host, httpx.AsyncClient(trust_env=False) as http:
            result = await http.post(host.url + "/v1/chat/completions", json=self.request(), headers={"Authorization": "Bearer " + host.token})
            self.assertEqual(host.failure, "provider_error")
            self.assertNotIn(SOURCE_ID, result.text)
            self.assertEqual(host.calls, {})
            self.assertEqual(state["seen_tool_ids"], [])

    async def test_controlled_catalog_model_and_choice_count_cannot_be_changed(self):
        engine = self.engine("openai"); state = self.state(engine)
        changes = [{"n": 2}, {"n": True}, {"model": "other"}, {"tools": [{"type": "web_search"}]},
            {"functions": [{"name": "Bash"}]}, {"web_search_options": {}}, {"audio": {}},
            {"tools": [{"type": "function", "function": {"name": "Bash"}}]}, {"mcp_servers": [{"url": "https://untrusted.invalid"}]}]
        async with httpx.AsyncClient(trust_env=False) as http:
            for change in changes:
                async with LocalHost(engine, state) as host:
                    result = await http.post(host.url + "/v1/chat/completions", json={**self.request(), **change}, headers={"Authorization": "Bearer " + host.token})
                    self.assertEqual(result.status_code, 400)
            self.assertFalse(self.records)

    async def test_absent_usage_retains_native_conservative_output_charge(self):
        from xuloom.agents.collaboration import DELEGATION_BUDGET, new_run
        engine = self.engine("openai"); state = self.state(engine); self.mode = "no_usage"
        budget = new_run(engine.tool_context["collaboration"])
        token = DELEGATION_BUDGET.set(budget)
        try:
            async with LocalHost(engine, state) as host, httpx.AsyncClient(trust_env=False) as http:
                result = await http.post(host.url + "/v1/chat/completions", json=self.request(), headers={"Authorization": "Bearer " + host.token})
                self.assertEqual(result.status_code, 200)
                self.assertIsNone(host.failure)
                self.assertEqual((budget["output_used"], budget["output_reserved"]), (100, 0))
        finally: DELEGATION_BUDGET.reset(token)


@unittest.skipUnless(MCP, "optional MCP runtime not installed")
class ExternalHostTests(ExternalFixture):
    async def test_exit_closes_model_and_mcp_admission_before_vendor_handshake(self):
        from mcp.client.streamable_http import streamable_http_client
        from mcp import ClientSession
        engine = self.engine(); state = self.state(engine)
        host = await LocalHost(engine, state).__aenter__()
        entered, release = asyncio.Event(), asyncio.Event()
        async def handshake():
            entered.set(); await release.wait()
        host.on_interrupt = handshake
        await host.grant_tool({"name": "mcp__xuloom__file_read_tool", "id": "exit_guard", "input": {"file_path": "evidence.txt"}})
        closing = asyncio.create_task(host.__aexit__(asyncio.CancelledError, asyncio.CancelledError(), None))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            async with httpx.AsyncClient(trust_env=False) as http:
                result = await http.post(host.url + "/v1/messages", json=self.request(), headers={"x-api-key": host.token})
                self.assertEqual(result.status_code, 409)
            async with httpx.AsyncClient(headers={"Authorization": "Bearer " + host.token}, trust_env=False) as transport, streamable_http_client(host.url + "/mcp", http_client=transport) as (read, write, _):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    result = await client.call_tool("file_read_tool", {"file_path": "evidence.txt", SOURCE_ID: "exit_guard"})
                    self.assertTrue(result.isError)
            self.assertFalse(self.records)
            self.assertFalse(engine.recent_tool_activity)
        finally:
            release.set(); await closing

    async def test_stop_closes_admission_for_a_mutation_already_waiting_at_native_gate(self):
        from mcp.client.streamable_http import streamable_http_client
        from mcp import ClientSession
        engine = self.engine(); write = engine.available_tools["file_write_tool"]; state = self.state(engine)
        engine.available_tools["file_write_tool"] = write
        execute, release, queued = write.execute, asyncio.Event(), asyncio.Event()
        async def held(**values):
            await release.wait()
            return await execute(**values)
        write.execute = held
        async with LocalHost(engine, state) as host:
            values = [{"file_path": str(self.root / name), "content": name} for name in ("one.txt", "two.txt")]
            for i, arguments in enumerate(values):
                await host.grant_tool({"name": "mcp__xuloom__file_write_tool", "id": "stop_" + str(i), "input": arguments})
            acquire, entered = asyncio.Lock.acquire, 0
            async def observed(lock):
                nonlocal entered
                if lock is host.execution_lock:
                    entered += 1
                    if entered == 2: queued.set()
                return await acquire(lock)
            async with httpx.AsyncClient(headers={"Authorization": "Bearer " + host.token}, trust_env=False) as transport, streamable_http_client(host.url + "/mcp", http_client=transport) as (read, stream, _):
                async with ClientSession(read, stream) as client:
                    await client.initialize()
                    with patch.object(asyncio.Lock, "acquire", observed):
                        first = asyncio.create_task(client.call_tool("file_write_tool", {**values[0], SOURCE_ID: "stop_0"}))
                        second = asyncio.create_task(client.call_tool("file_write_tool", {**values[1], SOURCE_ID: "stop_1"}))
                        await asyncio.wait_for(queued.wait(), 5)
                        host.stopping = True; release.set()
                        results = await asyncio.gather(first, second)
            self.assertFalse(results[0].isError)
            self.assertTrue(results[1].isError)
            self.assertTrue((self.root / "one.txt").is_file())
            self.assertFalse((self.root / "two.txt").exists())
            self.assertEqual(host.started_calls, {"stop_0"})

    async def test_concurrent_external_mutations_use_the_same_serial_native_boundary(self):
        from mcp.client.streamable_http import streamable_http_client
        from mcp import ClientSession
        engine = self.engine(); write = engine.available_tools["file_write_tool"]; state = self.state(engine)
        engine.available_tools["file_write_tool"] = write
        original, active, maximum = write.execute, 0, 0
        async def tracked(**values):
            nonlocal active, maximum
            active += 1; maximum = max(maximum, active)
            try:
                await asyncio.sleep(.05)
                return await original(**values)
            finally: active -= 1
        write.execute = tracked
        async with LocalHost(engine, state) as host:
            values = [{"file_path": str(self.root / (str(i) + ".txt")), "content": str(i)} for i in range(2)]
            for i, arguments in enumerate(values):
                await host.grant_tool({"name": "mcp__xuloom__file_write_tool", "id": "toolu_write_" + str(i), "input": arguments})
            async with httpx.AsyncClient(headers={"Authorization": "Bearer " + host.token}, trust_env=False) as transport, streamable_http_client(host.url + "/mcp", http_client=transport) as (read, write_stream, _):
                async with ClientSession(read, write_stream) as client:
                    await client.initialize()
                    results = await asyncio.gather(*(client.call_tool("file_write_tool", {**v, SOURCE_ID: "toolu_write_" + str(i)}) for i, v in enumerate(values)))
            self.assertTrue(all(not result.isError for result in results), results)
            self.assertEqual(maximum, 1)

    async def test_owned_mcp_uses_native_permission_and_reuses_a_durable_receipt(self):
        from mcp.client.streamable_http import streamable_http_client
        from mcp import ClientSession
        engine = self.engine(); state = self.state(engine)
        async with LocalHost(engine, state) as host:
            async with httpx.AsyncClient(trust_env=False) as http:
                result = await http.post(host.url + "/v1/messages", json=self.request(), headers={"x-api-key": host.token})
                self.assertEqual(result.status_code, 200); self.assertIn(SOURCE_ID, result.text)
            async with httpx.AsyncClient(headers={"Authorization": "Bearer " + host.token}, trust_env=False) as transport, streamable_http_client(host.url + "/mcp", http_client=transport) as (read, write, _):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    arguments = {"file_path": "evidence.txt", SOURCE_ID: "toolu_contract"}
                    first, second = await asyncio.gather(client.call_tool("file_read_tool", arguments), client.call_tool("file_read_tool", arguments))
                    self.assertFalse(first.isError); self.assertEqual(first.content, second.content)
                    self.assertIn("OWNED_EVIDENCE", str(first.content)); self.assertEqual(len(engine.recent_tool_activity), 1)
                    bogus = await client.call_tool("file_read_tool", {**arguments, SOURCE_ID: "invented"})
                    self.assertTrue(bogus.isError)
        self.assertEqual(host.usage["output_tokens"], 5)
        self.assertEqual(self.records[0]["headers"]["x-api-key"], "fixture-only")

    async def test_native_denial_is_not_overridden_by_external_autoallow(self):
        from mcp.client.streamable_http import streamable_http_client
        from mcp import ClientSession
        engine = self.engine(); state = self.state(engine)
        engine.permission_manager.always_deny_tools.add("file_read_tool")
        async with LocalHost(engine, state) as host:
            await host.grant_tool({"name": "mcp__xuloom__file_read_tool", "id": "toolu_denied", "input": {"file_path": "evidence.txt"}})
            async with httpx.AsyncClient(headers={"Authorization": "Bearer " + host.token}, trust_env=False) as transport, streamable_http_client(host.url + "/mcp", http_client=transport) as (read, write, _):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    result = await client.call_tool("file_read_tool", {"file_path": "evidence.txt", SOURCE_ID: "toolu_denied"})
                    self.assertTrue(result.isError); self.assertNotIn("OWNED_EVIDENCE", str(result.content))

    async def test_unknown_catalog_changed_model_and_origin_never_reach_provider(self):
        engine = self.engine(); state = self.state(engine)
        async with httpx.AsyncClient(trust_env=False) as http:
            for change, headers in (({"model": "changed"}, {}), ({"tools": [{"name": "Bash"}]}, {}),
                ({"tools": [{"name": "mcp__xuloom__file_read_tool", "type": "web_search_20250305"}]}, {}),
                ({"mcp_servers": [{"url": "https://untrusted.invalid"}]}, {}), ({}, {"Origin": "https://untrusted.invalid"})):
                async with LocalHost(engine, state) as host:
                    result = await http.post(host.url + "/v1/messages", json={**self.request(), **change}, headers={"x-api-key": host.token, **headers})
                    self.assertIn(result.status_code, {400, 401})
            self.assertEqual(self.records, [])

    async def test_checkpoint_failure_and_duplicate_identity_do_not_start_an_operation(self):
        engine = self.engine(); state = self.state(engine)
        async with LocalHost(engine, state) as host, httpx.AsyncClient(trust_env=False) as http:
            with patch.object(engine, "persist_session_state", return_value=False):
                result = await http.post(host.url + "/v1/messages", json=self.request(), headers={"x-api-key": host.token})
            self.assertEqual(result.status_code, 400); self.assertEqual(host.failure, "persistence_error"); self.assertEqual(self.records, [])
            block = {"name": "mcp__xuloom__file_read_tool", "id": "toolu_once", "input": {"file_path": "evidence.txt"}}
            await host.grant_tool(block)
            with self.assertRaisesRegex(ValueError, "repeated"): await host.grant_tool(block)
            self.assertEqual(engine.recent_tool_activity, [])

    async def test_cancel_with_full_stream_queue_does_not_deadlock(self):
        engine = self.engine(); state = self.state(engine); self.mode = "backpressure"
        async with LocalHost(engine, state) as host:
            queue, ready = asyncio.Queue(1), asyncio.get_running_loop().create_future()
            task = asyncio.create_task(host.forward(self.request(), {}, queue, ready))
            await asyncio.wait_for(ready, 3)
            while not queue.full(): await asyncio.sleep(.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await asyncio.wait_for(task, 1)

    async def test_truncated_provider_stream_is_an_explicit_failure(self):
        engine = self.engine(); state = self.state(engine); self.mode = "truncated"
        async with LocalHost(engine, state) as host, httpx.AsyncClient(trust_env=False) as http:
            result = await http.post(host.url + "/v1/messages", json=self.request(), headers={"x-api-key": host.token})
            self.assertIn("Owned provider stream interrupted", result.text)
            self.assertEqual(host.failure, "provider_error"); self.assertTrue(host.failed.is_set())


@unittest.skipUnless(MCP and PROGRAM, "set XULOOM_TEST_CLAUDE_PROGRAM for installed CLI / local API contracts")
class InstalledClaudeTests(ExternalFixture):
    def role(self):
        roles = self.root / ".xuloom/agents"; roles.mkdir(parents=True)
        (roles / "external-reader.md").write_text("---\nname: external-reader\ndescription: external controlled reader\nbackend: claude\nbackend_program: " + str(PROGRAM).replace("\\", "/") + "\ntools: [Read]\n---\nRead only the assigned evidence.\n", encoding="utf8")

    async def test_real_worker_followup_keeps_identity_budget_tools_and_deletable_history(self):
        self.role(); engine = self.engine()
        first = await engine.agents("spawn", prompt="Read evidence.txt", subagent_type="external-reader")
        self.assertTrue(first["ok"], first)
        row = first["data"]["agents"][0]; self.assertEqual(row["status"], "completed", row)
        state = engine.session_manager.load_subagent_transcript(row["agent_id"])
        second = await engine.agents("followup", agent_id=row["agent_id"], prompt="Explain that evidence briefly")
        self.assertTrue(second["ok"], second); self.assertEqual(second["data"]["status"], "completed")
        self.assertEqual(second["data"]["phase"], "completed")
        after = engine.session_manager.load_subagent_transcript(row["agent_id"])
        self.assertEqual(state["metadata"]["external_agent"]["session_id"], after["metadata"]["external_agent"]["session_id"])
        self.assertEqual(len(self.records), 3); self.assertEqual(engine.tool_context["collaboration_run"]["output_used"], 15)
        self.assertEqual(engine.tool_context["collaboration_run"]["output_reserved"], 0)
        self.assertEqual(len(after["metadata"]["tool_activity"]["recent"]), 1)
        self.assertTrue(all(r["headers"]["x-api-key"] == "fixture-only" for r in self.records))
        self.assertFalse(list((self.root / "data").rglob("connection.json")))
        await engine.aclose()
        self.assertTrue(engine.session_manager.delete_session(engine.session_id))

    async def test_provider_builtin_call_is_rejected_without_fallback(self):
        self.role(); self.mode = "builtin"; engine = self.engine()
        receipt = await engine.agents("spawn", prompt="Read evidence.txt", subagent_type="external-reader")
        row = receipt["data"]["agents"][0]
        self.assertEqual(row["status"], "crashed", row)
        self.assertEqual(row["receipt"]["reason"], "provider_error")
        self.assertEqual(len(self.records), 1)

    async def test_pending_message_is_delivered_before_external_worker_finishes(self):
        self.role(); self.mode = "hold"; engine = self.engine()
        spawned = await engine.agents("spawn", prompt="Read evidence.txt", subagent_type="external-reader", run_in_background=True)
        identity = spawned["data"]["agents"][0]["agent_id"]
        async def entered():
            while not self.records: await asyncio.sleep(.02)
        await asyncio.wait_for(entered(), 15)
        sent = await engine.agents("send", agent_id=identity, prompt="ADDITIONAL_EVIDENCE_REQUEST")
        self.assertTrue(sent["ok"], sent)
        self.mode = "normal"; self.release.set()
        await engine.agents("wait", agent_ids=[identity], timeout_ms=60000)
        row = next(row for row in engine.subagent_registry if row["agent_id"] == identity)
        self.assertEqual(row["status"], "completed", row)
        self.assertEqual(row["pending_messages"], [])
        self.assertEqual(len(self.records), 3)
        self.assertIn("ADDITIONAL_EVIDENCE_REQUEST", json.dumps(self.records[-1]["body"]))

    async def test_each_external_request_uses_the_existing_shared_output_grant(self):
        from xuloom.agents.collaboration import new_run
        self.role(); engine = self.engine()
        budget = new_run(engine.tool_context["collaboration"])
        budget["budget"]["output_tokens"] = 5; budget["request_cap"] = 5
        engine.tool_context["collaboration_run"] = budget
        receipt = await engine.agents("spawn", prompt="Read evidence.txt", subagent_type="external-reader")
        row = receipt["data"]["agents"][0]
        self.assertEqual(row["status"], "crashed", row)
        self.assertEqual(row["receipt"]["reason"], "budget_exhausted")
        self.assertEqual(len(self.records), 1); self.assertEqual(self.records[0]["body"]["max_tokens"], 5)
        self.assertEqual((budget["output_used"], budget["output_reserved"]), (5, 0))

    async def test_owner_cancel_reaps_owned_cli_and_marks_unconfirmed_resume(self):
        self.role(); self.mode = "hold"; engine = self.engine(); processes = []
        create = asyncio.create_subprocess_exec
        async def capture(*args, **kwargs):
            process = await create(*args, **kwargs); processes.append(process); return process
        with patch("asyncio.create_subprocess_exec", capture):
            spawned = await engine.agents("spawn", prompt="Read evidence.txt", subagent_type="external-reader", run_in_background=True)
            identity = spawned["data"]["agents"][0]["agent_id"]
            async def entered():
                while not self.records: await asyncio.sleep(.02)
            await asyncio.wait_for(entered(), 15)
            cancelled = await engine.agents("cancel", agent_id=identity)
            self.assertEqual(cancelled["data"]["status"], "cancelled")
        self.assertTrue(all(p.returncode is not None for p in processes))
        state = engine.session_manager.load_subagent_transcript(identity)
        self.assertEqual(state["metadata"]["external_agent"]["phase"], "interrupted")
        self.release.set(); self.mode = "normal"
        resumed = await engine.agents("followup", agent_id=identity, prompt="Continue")
        self.assertEqual(resumed["data"]["status"], "crashed")
        self.assertEqual(resumed["data"]["receipt"]["reason"], "external_outcome_unknown")
        self.assertEqual(len(self.records), 1)


class ExternalDefinitionTests(unittest.TestCase):
    def test_child_environment_has_no_ambient_credentials(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "private", "OPENAI_API_KEY": "private", "CLAUDE_CODE_OAUTH_TOKEN": "private", "AWS_SECRET_ACCESS_KEY": "private"}):
            self.assertNotIn("private", json.dumps(environment(Path(tempfile.gettempdir()))))

    def test_invalid_checkpoint_and_shell_launcher_do_not_execute(self):
        with self.assertRaises(ValueError): resolve("relative/claude")
        with self.assertRaises(ValueError): restore(type("Engine", (), {"tool_context": {}})(), {"external_agent": {"backend": "claude", "phase": "completed"}})


if __name__ == "__main__": unittest.main()
