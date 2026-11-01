"""Named native workers through real local SDK requests and saved lifecycle."""
import asyncio
import copy
import json
import unittest
from unittest.mock import patch

if __package__:
    from . import test_runtime_connections as fixtures
else:
    import test_runtime_connections as fixtures
from xuloom.core.engine import QueryEngine
from xuloom.core.run_result import RunResult
from xuloom.core.config import save_config
from xuloom.core.messages import create_user_message, create_assistant_message
from xuloom.desktop.capability_config import create_definition


class HeterogeneousAgentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.RuntimeConnectionTests()
        await self.fixture.asyncSetUp()
        self.config_patch = patch("xuloom.core.config.CONFIG_FILE", self.fixture.root / "config.json")
        self.config_patch.start()
        self.cfg = {"model_connections": [self.connection("a"), self.connection("b", "anthropic")], "active_model_connection": "a"}
        save_config(self.cfg)
        self.parent = self.fixture.engine("parent")
        self.tool = self.parent.available_tools["agent_tool"]
        self.roles = self.fixture.root / ".xuloom/agents"
        self.roles.mkdir(parents=True)
        self.role("reader-a", "a"); self.role("reader-b", "b")

    async def asyncTearDown(self):
        self.config_patch.stop()
        await self.fixture.asyncTearDown()

    def connection(self, label, provider="openai"):
        return {"id": label, "name": label, "provider": provider, "model": "opaque-model", "models": ["opaque-model"],
            "api_key": "key-" + label, "base_url": self.fixture.base + "/" + label + ("/v1" if provider == "openai" else ""), "network_mode": "direct"}

    def role(self, name, connection, model="inherit"):
        (self.roles / (name + ".md")).write_text("---\nname: " + name + "\ndescription: independent evidence\nconnection: " + connection + "\nmodel: " + model + "\ntools: []\n---\nCheck original evidence.\n", encoding="utf-8")

    async def run_child(self, child, user_input, sys_print_callback):
        await self.fixture.request(child)
        child.messages.extend([create_user_message(user_input), create_assistant_message([{"type": "text", "text": "done"}])])
        return RunResult("done", "completed", True)

    async def spawn(self, role, **kwargs):
        return json.loads(await self.tool.execute(prompt="independent check", subagent_type=role, response_format="json", **kwargs))

    async def test_different_protocols_and_accounts_are_not_parent_credentials(self):
        async def run(child, user_input, sys_print_callback):
            self.assertEqual(child.fallback_model, child.primary_model)
            return await self.run_child(child, user_input, sys_print_callback)
        with patch.object(QueryEngine, "run", run):
            results = await asyncio.gather(self.spawn("reader-a"), self.spawn("reader-b"))
        self.assertEqual(len(self.fixture.records), 2)
        for record in self.fixture.records:
            label = record["path"].split("/")[1]
            self.fixture.assert_credentials(record, label, "openai" if label == "a" else "anthropic")
        self.assertEqual({result["agents"][0]["status"] for result in results}, {"completed"})
        self.assertEqual({r["provider"] for r in self.parent.subagent_registry}, {"openai", "anthropic"})
        inventory = json.loads(await self.tool.execute(action="list"))["agents"]
        self.assertEqual({r["connection_id"] for r in inventory}, {"a", "b"})
        for row in inventory: self.assertNotIn("connection_binding", row)
        for path in (self.fixture.root / "data").rglob("*.json"):
            content = path.read_text(encoding="utf-8")
            self.assertNotIn("key-a", content); self.assertNotIn("key-b", content)

    async def test_same_url_model_keeps_exact_capabilities_and_does_not_inherit_parent_effort(self):
        self.cfg["model_connections"][0].update(model_inputs={"opaque-model": ["text", "image"]}, model_reasoning={"opaque-model": {"adapter": "openai", "efforts": ["low", "high"], "effort": "high"}})
        other = copy.deepcopy(self.cfg["model_connections"][0]); other.update(id="other", api_key="key-other", model_inputs={"opaque-model": ["text"]})
        other["model_reasoning"]["opaque-model"]["effort"] = "low"
        self.cfg["model_connections"].append(other); save_config(self.cfg); self.role("reader-other", "other")
        self.parent.reasoning_effort = "high"; self.parent._sync_model_runtime_context()
        async def run(child, user_input, sys_print_callback):
            self.assertEqual(child._input_contract(child.primary_model), ["text"])
            self.assertEqual(child.reasoning_profile["effort"], "low"); self.assertIsNone(child.reasoning_effort)
            image = self.fixture.root / "unread.png"; image.write_bytes(b"not an image")
            from xuloom.tools.file_read_tool import FileReadTool
            reader = FileReadTool(); reader.context = child.tool_context
            self.assertIn("不支持", await reader.execute(file_path=str(image)))
            return await self.run_child(child, user_input, sys_print_callback)
        with patch.object(QueryEngine, "run", run):
            result = await self.spawn("reader-other")
            self.assertEqual(result["agents"][0]["status"], "completed", result)
        self.assertEqual(self.fixture.records[0]["headers"]["authorization"], "Bearer key-other")

    async def test_invalid_binding_rejected_before_registration_and_creation(self):
        for connection, model in [("missing", "inherit"), ("a", "unlisted")]:
            self.role("invalid-reader", connection, model)
            with self.assertRaises(ValueError): await self.spawn("invalid-reader")
        self.cfg["model_connections"][0]["api_key"] = ""; save_config(self.cfg)
        with self.assertRaisesRegex(ValueError, "API key"): await self.spawn("reader-a")
        self.assertEqual(self.parent.subagent_registry, []); self.assertEqual(self.fixture.records, [])
        registry = self.parent.tool_context["skill_registry"]
        with self.assertRaises(ValueError):
            create_definition(registry, {"kind": "agents", "scope": "project", "name": "invalid-create", "description": "check", "prompt": "check", "connection": "missing"})
        self.assertFalse((self.roles / "invalid-create.md").exists())

    async def test_queue_rechecks_endpoint_and_never_falls_back_to_parent(self):
        await self.queue_change(change_address=True)

    async def test_queue_allows_same_connection_key_rotation(self):
        await self.queue_change(change_address=False)

    async def queue_change(self, *, change_address):
        self.parent.tool_context["skill_registry"].update(agent_concurrency=1); self.parent.set_collaboration_mode("auto")
        gate, started = asyncio.Event(), asyncio.Event()
        calls = 0
        async def run(child, user_input, sys_print_callback):
            nonlocal calls
            calls += 1
            if calls == 1: started.set(); await gate.wait()
            return await self.run_child(child, user_input, sys_print_callback)
        with patch.object(QueryEngine, "run", run):
            await self.spawn("reader-a", count=2, run_in_background=True)
            await asyncio.wait_for(started.wait(), 10)
            self.cfg["model_connections"][0]["api_key"] = "key-rotated"
            if change_address: self.cfg["model_connections"][0]["base_url"] = self.fixture.base + "/trap/v1"
            save_config(self.cfg); gate.set()
            await asyncio.gather(*self.parent.tool_context["subagent_tasks"].tasks.values())
        if change_address:
            self.assertEqual(calls, 1); self.assertEqual(len(self.fixture.records), 1)
            self.assertEqual(self.parent.subagent_registry[1]["status"], "crashed")
        else:
            self.assertEqual(calls, 2)
            self.assertEqual([r["headers"]["authorization"] for r in self.fixture.records], ["Bearer key-a", "Bearer key-rotated"])

    async def test_restart_queue_and_followup_keep_frozen_role_and_connection(self):
        self.parent.tool_context["skill_registry"].update(agent_concurrency=1); self.parent.set_collaboration_mode("auto")
        started, gate = asyncio.Event(), asyncio.Event()
        async def hold(child, user_input, sys_print_callback): started.set(); await gate.wait(); return await self.run_child(child, user_input, sys_print_callback)
        with patch.object(QueryEngine, "run", hold):
            await self.spawn("reader-b", count=2, run_in_background=True); await asyncio.wait_for(started.wait(), 10)
            saved = self.parent.session_manager.load_session_state(self.parent.session_id)
            queued = [r["agent_id"] for r in saved["metadata"]["subagents"] if r["status"] == "queued"]
            await self.parent.tool_context["subagent_tasks"].close()
        self.parent.session_manager.save_session_state(self.parent.session_id, saved["messages"], saved["metadata"])
        self.parent.session_manager.release_writer()
        restored = self.fixture.engine("new-parent"); self.assertTrue(restored.load_session(self.parent.session_id))
        recovered = restored.available_tools["agent_tool"]
        self.role("reader-b", "a", "unlisted")
        async def run(child, user_input, sys_print_callback): return await self.run_child(child, user_input, sys_print_callback)
        with patch.object(QueryEngine, "run", run):
            reply = json.loads(await recovered.execute(action="resume_queue", agent_ids=queued))
            self.assertEqual(reply["resumed"], 1)
            await asyncio.gather(*restored.tool_context["subagent_tasks"].tasks.values())
            followup = json.loads(await recovered.execute(action="followup", agent_id=queued[0], prompt="next", run_in_background=False))
        self.assertEqual(followup["status"], "completed", followup); self.assertEqual(followup["turn"], 2)
        self.assertEqual(len(self.fixture.records), 2)
        for record in self.fixture.records: self.fixture.assert_credentials(record, "b", "anthropic")
        self.cfg["model_connections"] = [self.cfg["model_connections"][0]]; save_config(self.cfg)
        with self.assertRaisesRegex(ValueError, "missing"):
            await recovered.execute(action="followup", agent_id=queued[0], prompt="not dispatched")
        self.assertEqual(len(self.fixture.records), 2)
