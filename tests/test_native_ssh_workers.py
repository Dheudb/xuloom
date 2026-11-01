"""Native AgentTool/FIFO to actual encrypted loopback SSH and HTTP model streams."""
import asyncio
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

from tests import test_remote_ssh as remote, ssh_fixture
from xuloom.agents.registry import AgentRegistry
from xuloom.core.engine import QueryEngine


@unittest.skipUnless(ssh_fixture.paramiko is not None and shutil.which("ssh"), "Development SSH fixture and OpenSSH required")
class NativeSSHTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = remote.SSHWireTests(); await self.fixture.asyncSetUp()
        self.root = self.fixture.root
        self.config_file = self.root / "parent-config.json"
        self.config_file.write_text(json.dumps({"ssh_connections": [self.fixture.profile], "model_connections": []}), encoding="utf-8")
        self.config = patch("xuloom.core.config.CONFIG_FILE", self.config_file); self.config.start()
        self.paths = patch.dict("os.environ", {"XULOOM_SKILLS_PATHS": ""}); self.paths.start()
        self.engine = QueryEngine(model="parent-model", model_provider="openai", api_key="parent-private-never-transfer",
            api_base_url=self.fixture.local.base + "/parent/v1", cwd=str(self.root))
        self.engine.permission_manager.approval_policy.set_mode("full")
        self.tool = self.engine.available_tools["agent_tool"]
        directory = self.root / ".xuloom" / "agents"; directory.mkdir(parents=True)
        self.role = directory / "remote-review.md"
        self.role.write_text("---\nname: remote-review\ndescription: Independent host review\nexecutor: ssh:test\nconnection: remote\nmcp_servers: []\n---\nComplete the independent assignment on this execution host.\n", encoding="utf-8")

    async def asyncTearDown(self):
        try:
            await self.engine.aclose()
        finally:
            self.paths.stop(); self.config.stop(); await self.fixture.asyncTearDown()

    async def spawn(self, **kwargs):
        return json.loads(await self.tool.execute(prompt="Say hello from your execution host", subagent_type="remote-review",
            response_format="json", **kwargs))

    async def test_native_role_uses_host_key_and_saved_remote_history_for_followup(self):
        answer = await self.spawn(run_in_background=False)
        row = answer["agents"][0]; identity = row["agent_id"]
        self.assertEqual(row["status"], "completed", row); self.assertEqual(row["result"], "answer-remote")
        saved = self.tool._worker(identity)
        self.assertEqual(saved["executor"], "ssh:test"); self.assertEqual(saved["model"], "opaque-model")
        reference = saved["remote_reference"]; self.assertTrue(reference["url"].startswith("xuloom://chat/ssh/v1/"))
        transcript = self.engine.session_manager.load_subagent_transcript(identity)
        self.assertEqual(transcript["result"], row["result"]); self.assertEqual(transcript["remote_reference"], reference)
        for record in self.fixture.local.records:
            self.assertTrue(record["path"].startswith("/remote/"))
            self.assertEqual({k.lower(): v for k, v in record["headers"].items()}["authorization"], "Bearer key-remote")
        continued = json.loads(await self.tool.execute(action="followup", agent_id=identity, prompt="Now give one more greeting", run_in_background=False))
        self.assertEqual(continued["status"], "completed", continued)
        self.assertEqual(continued["remote_reference"]["session_id"], reference["session_id"])
        self.assertEqual(len(self.fixture.local.records), 2)
        self.assertTrue(any("Say hello" in str(message.get("content")) for message in self.fixture.local.records[-1]["body"]["messages"]))
        visible = json.loads(await self.tool.execute(action="list"))
        token = saved["dispatch"]["remote"]["open"]["owner"]["token"]
        for secret in (token, "key-remote", "parent-private-never-transfer"):
            self.assertNotIn(secret, json.dumps(visible)); self.assertNotIn(secret, json.dumps(answer))
        self.assertEqual(self.engine.tool_context["collaboration_run"]["output_reserved"], 0)
        self.assertTrue(self.engine.tool_context["subagent_tasks"].backends[identity].client.closed)

    async def test_isolated_parent_and_invalid_executor_never_start_an_ssh_connection(self):
        self.engine.execution_sandbox.mode = "workspace"
        with self.assertRaisesRegex(ValueError, "no host fallback"):
            await self.spawn(run_in_background=False)
        self.assertEqual(self.engine.subagent_registry, []); self.assertEqual(self.fixture.local.records, [])
        self.engine.execution_sandbox.mode = "host"
        self.role.write_text(self.role.read_text().replace("ssh:test", "ssh:test;untrusted"), encoding="utf-8")
        entry = next(row for row in AgentRegistry(str(self.root)).discover() if row["name"] == "remote-review")
        self.assertFalse(entry["valid"])

    async def test_remote_workers_use_the_native_fifo_and_do_not_require_model_batching(self):
        self.engine.tool_context["skill_registry"].update(agent_concurrency=1)
        self.engine.set_collaboration_mode("auto")
        started = self.fixture.hold_model()
        answer = await self.spawn(count=3, run_in_background=True)
        self.assertEqual(len(answer["agents"]), 3)
        self.assertTrue(await asyncio.to_thread(started.wait, 10))
        tasks = self.engine.tool_context["subagent_tasks"]
        self.assertEqual(len(tasks.running), 1); self.assertEqual(len(tasks.pending), 2)
        self.assertEqual(len(self.fixture.local.records), 1)
        self.fixture.release.set()
        await asyncio.wait_for(asyncio.gather(*tasks.tasks.values()), 60)
        rows = json.loads(await self.tool.execute(action="list"))["agents"]
        self.assertEqual(len({row["agent_id"] for row in rows}), 3)
        self.assertTrue(all(row["status"] == "completed" for row in rows), rows)

    async def test_live_parent_denial_reaches_the_actual_remote_tool(self):
        self.fixture.hold_model(write=True)
        self.engine.permission_manager.always_deny_tools.add("file_write_tool")
        result = await self.spawn(run_in_background=False)
        self.assertEqual(result["agents"][0]["status"], "completed", result)
        self.assertFalse((self.fixture.remote / "must-not-exist.txt").exists())
        self.assertEqual(len(self.fixture.local.records), 2)
        self.assertTrue(any("Delegating session" in str(row.get("content")) for row in self.fixture.local.records[-1]["body"]["messages"] if row["role"] == "tool"))

    async def test_message_sent_during_stream_is_consumed_before_native_completion(self):
        started = self.fixture.hold_model()
        answer = await self.spawn(run_in_background=True); identity = answer["agents"][0]["agent_id"]
        self.assertTrue(await asyncio.to_thread(started.wait, 10))
        sent = json.loads(await self.tool.execute(action="send", agent_id=identity, prompt="HOST_MESSAGE_MUST_BE_CONSUMED"))
        self.assertEqual(sent["delivery"], "host_inbox"); self.assertFalse(sent["requires_followup"])
        self.assertEqual(len(self.tool._mailbox(identity)), 1, "Receiving a message is not consuming it")
        self.fixture.release.set()
        await asyncio.wait_for(self.engine.tool_context["subagent_tasks"].tasks[identity], 45)
        self.assertEqual(len(self.fixture.local.records), 2)
        self.assertIn("HOST_MESSAGE_MUST_BE_CONSUMED", str(self.fixture.local.records[1]["body"]["messages"]))
        self.assertEqual(self.tool._mailbox(identity), [])
        self.assertEqual(self.tool._worker(identity)["status"], "completed")
        before = len(self.fixture.local.records)
        idle = json.loads(await self.tool.execute(action="send", agent_id=identity, prompt="NEXT_FOLLOWUP_ONLY"))
        self.assertTrue(idle["requires_followup"]); self.assertEqual(len(self.fixture.local.records), before)
        await self.tool.execute(action="followup", agent_id=identity, prompt="Continue with the queued message", run_in_background=False)
        self.assertIn("NEXT_FOLLOWUP_ONLY", str(self.fixture.local.records[-1]["body"]["messages"]))
        self.assertEqual(self.tool._mailbox(identity), [])

    async def test_role_skills_resolve_only_on_the_execution_host_and_target_changes_do_not_redirect(self):
        path = self.root / "remote-home/.xuloom/skills/host-note/SKILL.md"; path.parent.mkdir(parents=True)
        path.write_text("---\nname: host-note\ndescription: Host-only skill\n---\nHOST_ONLY_SKILL_INSTRUCTIONS\n", encoding="utf-8")
        self.role.write_text(self.role.read_text().replace("mcp_servers: []", "mcp_servers: []\nskills: [host-note]"), encoding="utf-8")
        self.assertTrue(AgentRegistry(str(self.root)).resolve("remote-review")["skills"])
        answer = await self.spawn(run_in_background=False); row = answer["agents"][0]
        self.assertEqual(row["status"], "completed", row)
        self.assertIn("HOST_ONLY_SKILL_INSTRUCTIONS", str(self.fixture.local.records[0]["body"]["messages"]))
        original = self.config_file.read_text(); config = json.loads(original); config["ssh_connections"][0]["cwd"] += "-other"
        try:
            self.config_file.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "target changed"):
                await self.tool.execute(action="followup", agent_id=row["agent_id"], prompt="Must not redirect", run_in_background=False)
            self.assertEqual(len(self.fixture.local.records), 1)
        finally:
            self.config_file.write_text(original, encoding="utf-8")

    async def test_cancel_confirms_the_owned_host_and_releases_native_capacity(self):
        started = self.fixture.hold_model(); answer = await self.spawn(run_in_background=True)
        identity = answer["agents"][0]["agent_id"]
        self.assertTrue(await asyncio.to_thread(started.wait, 10))
        cancelled = json.loads(await self.tool.execute(action="cancel", agent_id=identity))
        self.assertEqual(cancelled["status"], "cancelled", self.tool._worker(identity))
        self.assertFalse(self.tool._worker(identity).get("outcome_unknown"))
        self.assertEqual(self.engine.tool_context["subagent_tasks"].running, set())
        self.assertEqual(len(self.fixture.local.records), 1)

    async def test_explicit_output_budget_limits_actual_remote_model_requests(self):
        self.engine.tool_context["skill_registry"].update(agent_limits={"output_tokens": 12, "request_output_tokens": 7})
        self.engine.set_collaboration_mode("auto")
        first = (await self.spawn(run_in_background=False))["agents"][0]
        second = json.loads(await self.tool.execute(action="followup", agent_id=first["agent_id"], prompt="One more", run_in_background=False))
        self.assertEqual(second["status"], "completed", second)
        self.assertEqual([row["body"].get("max_tokens", row["body"].get("max_completion_tokens")) for row in self.fixture.local.records], [7, 5])
        with self.assertRaisesRegex(ValueError, "预算"):
            await self.tool.execute(action="followup", agent_id=first["agent_id"], prompt="No remaining output", run_in_background=False)
        self.assertEqual(len(self.fixture.local.records), 2)

    async def test_parent_budget_checkpoint_failure_never_grants_a_remote_model_request(self):
        original = self.tool._write_subagent_registry_record
        failed = False
        async def write(record):
            nonlocal failed
            if not failed and self.engine.tool_context["collaboration_run"]["output_reserved"]:
                failed = True
                raise OSError("Controlled parent checkpoint failure")
            return await original(record)
        with patch.object(self.tool, "_write_subagent_registry_record", write):
            answer = await self.spawn(run_in_background=False)
        self.assertTrue(failed); self.assertNotEqual(answer["agents"][0]["status"], "completed", answer)
        self.assertEqual(self.fixture.local.records, [])
