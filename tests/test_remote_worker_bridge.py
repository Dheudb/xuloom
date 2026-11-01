"""Native worker RPC, actual SDK/SSH requests to loopback fixtures only."""
import asyncio
import copy
import json
from pathlib import Path
import unittest
import uuid
from unittest.mock import patch

from xuloom.core.run_result import PersistenceError
from xuloom.remote.worker_bridge import WorkerBridge
from xuloom.remote.ssh import SSHClient, RemoteRequestError
from xuloom.security.delegation import authorize_delegated, request_from_wire
from xuloom.security.permissions import ApprovalPolicy, PermissionDecision, PermissionManager
from tests import test_runtime_connections as runtime
from tests import test_remote_ssh as ssh_tests
from tests import ssh_fixture
import shutil


def assignment():
    return {"owner": {"token": uuid.uuid4().hex, "parent_session_id": "parent-session", "parent_agent_id": "parent-agent",
        "agent_id": "subagent-" + str(uuid.uuid4()), "depth": 1}, "role": {"agent_type": "fixture", "agent_role": "fixture",
        "system_prompt": "Complete only this independent assignment.", "skills": [], "allowed_tools": ["*"], "mcp_servers": []}}


class WorkerBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.local = runtime.RuntimeConnectionTests(); await self.local.asyncSetUp()
        self.root = self.local.root
        self.work = self.root / "host"; self.work.mkdir()
        directory = self.root / "config"; directory.mkdir(exist_ok=True)
        (directory / "config.json").write_text(json.dumps({"active_model_connection": "host", "model_connections": [{"id": "host", "provider": "openai",
            "model": "opaque-model", "models": ["opaque-model"], "api_key": "HOST_PRIVATE_KEY", "base_url": self.local.base + "/host/v1"}]}), encoding="utf-8")
        self.config = patch("xuloom.core.config.CONFIG_FILE", directory / "config.json"); self.config.start()
        self.paths = patch.dict("os.environ", {"XULOOM_SKILLS_PATHS": ""}); self.paths.start()
        self.events, self.bridges = [], []
        self.value = assignment()

    def bridge(self):
        bridge = WorkerBridge(self.events.append, self.work); self.bridges.append(bridge); return bridge

    async def asyncTearDown(self):
        for bridge in self.bridges:
            await bridge.close()
        self.paths.stop(); self.config.stop()
        await self.local.asyncTearDown()

    async def test_open_is_idempotent_keyless_and_cannot_switch_to_a_foreign_role_or_session(self):
        bridge = self.bridge(); first = await bridge.dispatch("worker.open", self.value)
        self.assertEqual(first, await bridge.dispatch("worker.open", self.value)); self.assertEqual(self.local.records, [])
        self.assertNotIn("HOST_PRIVATE_KEY", json.dumps(first)); self.assertNotIn(self.value["owner"]["token"], json.dumps(first))
        changed = copy.deepcopy(self.value); changed["role"]["system_prompt"] = "Changed"
        with self.assertRaises(ValueError): await bridge.dispatch("worker.open", changed)
        with self.assertRaises(ValueError): await self.bridge().dispatch("worker.open", changed)
        for method in ("prompt", "session.activate", "files.read", "config.save"):
            with self.assertRaises(ValueError): await bridge.dispatch(method, {})
        self.assertEqual(self.local.records, [])

    async def test_actual_native_run_saves_completion_and_reopen_does_not_replay(self):
        bridge = self.bridge(); opened = await bridge.dispatch("worker.open", self.value)
        self.assertEqual(opened["model_binding"]["api_base_url"], self.local.base + "/host/v1")
        params = {"run_id": str(uuid.uuid4()), "text": "Say hello on the configured execution host"}
        await bridge.dispatch("worker.start", params); await asyncio.wait_for(bridge.turn_task, 30)
        snapshot = await bridge.dispatch("worker.status", {})
        self.assertEqual(snapshot["phase"], "completed", snapshot)
        self.assertTrue(snapshot["run"]["receipt"]["persisted"])
        self.assertEqual(snapshot["session_id"], opened["session_id"])
        self.assertEqual(snapshot["run"]["receipt"]["text"], "answer-host")
        self.assertEqual({k.lower(): v for k, v in self.local.records[0]["headers"].items()}["authorization"], "Bearer HOST_PRIVATE_KEY")
        before = len(self.local.records)
        await bridge.close()
        reopened = self.bridge(); await reopened.dispatch("worker.open", self.value)
        self.assertTrue((await reopened.dispatch("worker.start", params))["reused"])
        self.assertEqual(len(self.local.records), before)
        with self.assertRaises(ValueError): await reopened.dispatch("worker.start", {**params, "text": "Different task"})
        self.assertNotIn("HOST_PRIVATE_KEY", json.dumps(self.events))

    async def test_initial_persistence_failure_never_starts_a_model(self):
        bridge = self.bridge(); await bridge.dispatch("worker.open", self.value)
        engine = await bridge.ensure_engine(write=True)
        with patch.object(engine, "persist_session_state", return_value=False):
            with self.assertRaises(PersistenceError):
                await bridge.dispatch("worker.start", {"run_id": str(uuid.uuid4()), "text": "Must not start"})
        self.assertEqual(self.local.records, []); self.assertIsNone(bridge.turn_task)

    async def test_required_missing_host_tools_and_mcp_never_fall_back_or_request_a_model(self):
        for policy in ({"allowed_tools": ["missing_host_tool"]}, {"mcp_servers": ["missing-host-service"]}):
            bridge = self.bridge(); value = assignment(); value["role"].update(policy)
            await bridge.dispatch("worker.open", value)
            with self.assertRaisesRegex(ValueError, "Remote role"):
                await bridge.dispatch("worker.start", {"run_id": str(uuid.uuid4()), "text": "Must not start without required host capabilities"})
            self.assertIsNone(bridge.turn_task)
        self.assertEqual(self.local.records, [])

    async def test_stale_remote_binding_cannot_silently_switch_to_a_different_endpoint(self):
        bridge = self.bridge(); await bridge.dispatch("worker.open", self.value)
        from xuloom.core.config import load_config, save_config
        config = load_config(); config["model_connections"][0]["base_url"] = self.local.base + "/different/v1"; save_config(config)
        with self.assertRaisesRegex(ValueError, "connection changed"):
            await bridge.dispatch("worker.start", {"run_id": str(uuid.uuid4()), "text": "Do not reroute this worker"})
        self.assertEqual(self.local.records, []); self.assertIsNone(bridge.turn_task)

    async def test_corrupt_saved_worker_is_not_replaced_with_an_empty_execution(self):
        bridge = self.bridge(); opened = await bridge.dispatch("worker.open", self.value)
        from xuloom.core.session import SessionManager
        with SessionManager(cwd=str(self.work)) as manager:
            path = Path(manager.session_dir) / (opened["session_id"] + ".json")
        path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError): await self.bridge().dispatch("worker.open", self.value)
        self.assertEqual(path.read_text(), "{broken"); self.assertEqual(self.local.records, [])

    async def test_unrelated_host_history_is_not_exposed_and_changed_control_is_rejected(self):
        bridge = self.bridge(); opened = await bridge.dispatch("worker.open", self.value)
        from xuloom.core.session import SessionManager
        with SessionManager(cwd=str(self.work)) as manager:
            other = str(uuid.uuid4()); manager.acquire_writer(other)
            self.assertTrue(manager.save_session_state(other, [{"role": "user", "content": "PRIVATE_OTHER_CONVERSATION"}], {}))
            path = Path(manager.session_dir) / (opened["session_id"] + ".json")
        self.assertEqual([row["id"] for row in bridge.sessions()], [opened["session_id"]])
        original = json.loads(path.read_text(encoding="utf-8"))
        for kind in ("role", "receipt"):
            raw = copy.deepcopy(original); saved = raw["metadata"]["delegation"]["remote_worker"]
            if kind == "role": saved["role"]["allowed_tools"] = ["bash_tool"]
            else: saved["run"] = {"id": "pretend", "input_digest": "0" * 64, "phase": "completed", "receipt": {"ok": True, "persisted": False}}
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(ValueError): await bridge.dispatch("worker.status", {})
            with self.assertRaises(ValueError): await self.bridge().dispatch("worker.open", self.value)
        self.assertEqual(self.local.records, [])


@unittest.skipUnless(ssh_fixture.paramiko is not None and shutil.which("ssh"), "Development SSH fixture and OpenSSH are required")
class WorkerSSHTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = ssh_tests.SSHWireTests(); await self.fixture.asyncSetUp()
        self.events = []

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def connect(self):
        client = SSHClient(self.fixture.profile, self.events.append, worker=True)
        self.fixture.clients.append(client)
        info = await client.start(); self.assertEqual(info["worker_protocol"], 1)
        return client

    async def wait_event(self, kind):
        async def wait():
            while not any(row.get("type") == kind for row in self.events): await asyncio.sleep(.01)
            return next(row for row in self.events if row.get("type") == kind)
        return await asyncio.wait_for(wait(), 40)

    async def test_encrypted_worker_uses_remote_credentials_and_owned_saved_completion(self):
        client = await self.connect(); value = assignment()
        opened = await client.request("worker.open", value)
        self.assertEqual(self.fixture.local.records, [])
        params = {"run_id": str(uuid.uuid4()), "text": "Hello from an independent remote worker"}
        await client.request("worker.start", params)
        event = await self.wait_event("worker_finished")
        self.assertEqual(event["result"]["phase"], "completed", event)
        self.assertEqual(event["result"]["run"]["receipt"]["text"], "answer-remote")
        self.assertEqual(event["result"]["session_id"], opened["session_id"])
        self.assertEqual({k.lower(): v for k, v in self.fixture.local.records[0]["headers"].items()}["authorization"], "Bearer key-remote")
        before = len(self.fixture.local.records)
        await client.aclose(); reader = await self.connect(); await reader.request("worker.open", value)
        self.assertTrue((await reader.request("worker.start", params))["reused"])
        self.assertEqual(len(self.fixture.local.records), before)
        self.assertNotIn("key-remote", json.dumps(self.events))

    async def test_second_channel_cannot_replace_or_claim_to_stop_the_owned_running_worker(self):
        started = self.fixture.hold_model()
        owner = await self.connect(); value = assignment(); await owner.request("worker.open", value)
        params = {"run_id": str(uuid.uuid4()), "text": "Wait for the controlled local stream"}
        await owner.request("worker.start", params)
        self.assertTrue(await asyncio.to_thread(started.wait, 5))
        reader = await self.connect(); snapshot = await reader.request("worker.open", value)
        self.assertEqual(snapshot["phase"], "owner_busy"); self.assertFalse(snapshot["connected_execution"])
        self.assertTrue((await reader.request("worker.start", params))["reused"])
        with self.assertRaises(RemoteRequestError) as error:
            await reader.request("worker.start", {"run_id": str(uuid.uuid4()), "text": "Never replace the live execution"})
        self.assertEqual(error.exception.code, "session_busy")
        response = await reader.request("stop")
        self.assertFalse(response["stopped"]); self.assertTrue(response["outcome_unknown"])
        self.assertTrue((await owner.request("worker.status"))["connected_execution"])
        stopped = await owner.request("stop")
        self.assertTrue(stopped["stopped"]); self.assertFalse(stopped["outcome_unknown"])
        self.assertEqual(stopped["worker"]["phase"], "cancelled")
        before = len(self.fixture.local.records)
        self.assertEqual((await reader.request("worker.status"))["phase"], "cancelled")
        self.assertTrue((await reader.request("worker.start", params))["reused"])
        self.assertEqual(len(self.fixture.local.records), before)

    async def test_real_remote_tool_waits_for_live_parent_authority_even_under_full_access(self):
        fixture = self.fixture
        target = fixture.remote / "not-authorized.txt"
        records = fixture.local.records
        def scripted(handler):
            body = json.loads(handler.rfile.read(int(handler.headers["Content-Length"])))
            records.append({"body": body, "headers": dict(handler.headers), "path": handler.path})
            tool_seen = any(message.get("role") == "tool" for message in body["messages"])
            handler.send_response(200); handler.send_header("Content-Type", "text/event-stream"); handler.end_headers()
            if not tool_seen:
                delta = {"tool_calls": [{"index": 0, "id": "worker-write", "type": "function", "function": {"name": "file_write_tool", "arguments": json.dumps({"file_path": str(target), "content": "must-not-be-written"})}}]}
                end = "tool_calls"
            else:
                delta, end = {"content": "Parent denied the write; no file was created."}, "stop"
            frames = [{"choices": [{"index": 0, "delta": delta, "finish_reason": None}]}, {"choices": [{"index": 0, "delta": {}, "finish_reason": end}]}]
            handler.wfile.write(("".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n").encode())
        with patch.object(fixture.local.server.RequestHandlerClass, "do_POST", scripted):
            client = await self.connect(); await client.request("worker.open", assignment())
            await client.request("worker.start", {"run_id": str(uuid.uuid4()), "text": "Try the assigned write once and report its real permission result"})
            event = await self.wait_event("worker_permission")
            self.assertFalse(target.exists()); self.assertEqual(len(records), 1)
            policy = PermissionManager(config_path=str(fixture.root / "parent-policy.json"), approval_policy=ApprovalPolicy("full"))
            policy.always_deny_tools.add("file_write_tool")
            automatic = PermissionDecision(**event["automatic"]) if event["automatic"] else None
            answer = await authorize_delegated(policy, request_from_wire(event["request"]), automatic, event["aliases"], remote={"host": fixture.profile["host"], "cwd": str(fixture.remote)})
            self.assertEqual(answer["behavior"], "deny")
            await client.request("respond", {"requestId": event["requestId"], "answer": answer})
            finished = await self.wait_event("worker_finished")
            self.assertEqual(finished["result"]["phase"], "completed", finished)
            self.assertFalse(target.exists()); self.assertEqual(len(records), 2, [r["body"]["messages"] for r in records])
