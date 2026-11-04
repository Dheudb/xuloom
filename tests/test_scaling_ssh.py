"""Actual encrypted SSH -> detached node daemon -> actual local SSE SDK calls."""
import asyncio
import json
import shutil
import time
import threading
import unittest
from unittest.mock import patch

from tests import ssh_fixture, test_remote_ssh
from xuloom.scaling.control import Controller
from xuloom.scaling.store import Store
from xuloom.scaling.node import Node


@unittest.skipUnless(ssh_fixture.paramiko is not None and shutil.which("ssh"), "Local encrypted SSH fixture required")
class ScalingSSHTests(unittest.IsolatedAsyncioTestCase):
    async def test_remote_own_api_configuration_and_native_saved_history(self):
        await self.run_cluster(False)

    async def test_inherited_api_and_autonomous_children_survive_ssh_disconnect(self):
        await self.run_cluster(True)

    async def run_cluster(self, hierarchical):
        fixture = test_remote_ssh.SSHWireTests(); await fixture.asyncSetUp()
        control = None
        release = threading.Event()
        if not hierarchical: release.set()
        def respond(handler, body):
            texts = [m.get("content", "") for m in body.get("messages", []) if m.get("role") == "user"]
            text = texts[-1] if texts else ""
            if isinstance(text, list): text = "\n".join(b.get("text", "") for b in text)
            if "SCALING DIALOGUE" in text:
                owner=text.split("\nOwner message:\n",1)[1].split("\nRuntime reference data",1)[0]
                answer={"decision":"redirect" if owner.startswith("change") else "reply","reply":"Primary reply: "+owner,
                    "goal":"Changed evidence direction","groups":[{"label":"Changed direction","prompt":"Collect evidence for the revised direction"}]}
            elif "SCALING PLAN" in text: answer = {"groups": [{"label": "Evidence", "prompt": "Find independent evidence"}]}
            elif "SCALING EXPAND" in text: answer = {"tasks": [{"label": str(i), "prompt": "Examine evidence branch " + str(i)} for i in range(3)]}
            elif "SCALING SYNTHESIZE" in text or "SCALING REVIEW" in text: answer = {"decision": "complete", "report": "Three verified fixture responses", "groups": []}
            else: answer = None
            if hierarchical and answer is None and "SCALING GROUP" not in text:
                release.wait(60)
            text = json.dumps(answer) if answer else "Fixture evidence and provenance"
            handler.send_response(200); handler.send_header("Content-Type", "text/event-stream"); handler.end_headers()
            frames = [{"choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]},
                      {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]
            handler.wfile.write(("".join("data: " + json.dumps(f) + "\n\n" for f in frames) + "data: [DONE]\n\n").encode())
            return True
        fixture.local.response_handler = respond
        config_file = fixture.root / "primary-config.json"
        config_file.write_text(json.dumps({"model_connections": [{"id": "primary", "provider": "openai", "model": "opaque-model", "models": ["opaque-model"],
            "api_key": "key-primary-private", "base_url": fixture.local.base + "/primary/v1"}], "ssh_connections": [fixture.profile],
            "scaling_nodes": [{"id": "loopback", "ssh": "test", "max_agents": 3, "automatic": True, "reserve_percent": 10, "backend": "native"}]}), encoding="utf-8")
        try:
            with patch("xuloom.core.config.CONFIG_FILE", config_file):
                from tests.test_scaling import resources
                control = Controller(Store(fixture.root / "control.sqlite3"), Node(Store(fixture.root / "primary.sqlite3"), resources=resources), hierarchical=hierarchical)
                await control.start()
                run = await control.dispatch("create", {"goal": "Collect three independent evidence branches", "nodes": ["loopback"], "accept_high_usage": True})
                if hierarchical:
                    end = time.monotonic() + 40
                    while time.monotonic() < end:
                        leaders = [r for r in control.store.walk("task", run["id"]) if r["kind"] == "leader" and r["status"] == "running"]
                        if leaders: break
                        await asyncio.sleep(.1)
                    self.assertTrue(leaders, await control.dispatch("status", {"id":run["id"]}))
                    control.loop_task.cancel()
                    await asyncio.gather(control.loop_task, return_exceptions=True)
                    await asyncio.gather(*list(control.jobs.values()), return_exceptions=True)
                    for client in list(control.clients.values()): await client.aclose()
                    control.clients.clear()
                    release.set()
                    remote_store = Store(fixture.root / "remote-data/scaling/node.sqlite3")
                    end = time.monotonic() + 60
                    while time.monotonic() < end:
                        result = remote_store.get("execution", leaders[0]["id"])
                        if result["status"] in {"completed", "failed"}: break
                        await asyncio.sleep(.15)
                    self.assertEqual(result["status"], "completed", result)
                    self.assertNotEqual(control.store.get("run", run["id"])["status"], "completed")
                    control.loop_task = asyncio.create_task(control.loop())
                end = time.monotonic() + 70
                while time.monotonic() < end:
                    row = await control.dispatch("status", {"id": run["id"]})
                    if row["status"] in {"completed", "blocked"}: break
                    await asyncio.sleep(.2)
                self.assertEqual(row["status"], "completed", row)
                tasks = list(control.store.walk("task", run["id"]))
                if hierarchical:
                    leader = next(t for t in tasks if t["kind"] == "leader")
                    nested = await control.dispatch("children", {"id":run["id"], "task":leader["id"]})
                    self.assertEqual(len([t for t in nested if t["kind"] == "explore"]), 3)
                    child = next(t for t in nested if t["kind"] == "explore")
                    body = await control.dispatch("child_result", {"id":run["id"], "task":leader["id"], "child":child["id"]})
                    self.assertIn("Fixture evidence", body["receipt"]["text"])
                else: self.assertEqual(len([t for t in tasks if t["kind"] == "explore"]), 3)
                self.assertTrue(all(t["receipt"]["persisted"] and t["session_id"] for t in tasks))
                self.assertEqual(len(fixture.server.commands), 2 if hierarchical else 1, "one multiplexed SSH gateway, not one process per worker")
                self.assertNotIn("key-primary-private", str(fixture.server.commands))
                self.assertNotIn("key-remote", str(fixture.server.commands))
                remote = [r for r in fixture.local.records if r["path"].startswith("/remote/")]
                primary = [r for r in fixture.local.records if r["path"].startswith("/primary/")]
                self.assertTrue(primary)
                self.assertEqual(bool(remote), not hierarchical)
                self.assertTrue(all(r["headers"]["authorization"] == "Bearer key-remote" for r in remote))
                self.assertTrue(all(r["headers"]["authorization"] == "Bearer key-primary-private" for r in primary))
                if hierarchical:
                    for index,text in enumerate(("What is the progress?","change the evidence direction")):
                        posted=await control.dispatch("message",{"id":run["id"],"text":text,"request_id":"owner-"+str(index)})
                        end=time.monotonic()+50
                        while time.monotonic()<end:
                            message=control.store.get("message",posted["id"])
                            if message["status"] in {"applied","failed"}:break
                            await asyncio.sleep(.15)
                        self.assertEqual(message["status"],"applied",message)
                        self.assertEqual(message["session_id"],next(t for t in tasks if t["kind"]=="plan")["session_id"])
                    end=time.monotonic()+60
                    while time.monotonic()<end:
                        revised=await control.dispatch("status",{"id":run["id"]})
                        if revised["round"]==2 and revised["status"] in {"completed","blocked"}:break
                        await asyncio.sleep(.15)
                    self.assertEqual(revised["status"],"completed",revised)
                    self.assertEqual(revised["goal"],"Changed evidence direction")
                    self.assertEqual(len(await control.dispatch("messages",{"id":run["id"]})),2)
                # End only the test-owned idle node service, never arbitrary PIDs.
                node = control.store.get("run", run["id"])["nodes"][1]
                await control.remote(node, "service.stop", {})
        finally:
            release.set()
            if control:
                try:
                    for run in control.store.walk("run", statuses=["running", "blocked", "paused"]):
                        await control.dispatch("cancel", {"id": run["id"]})
                        await control.tick_guard(control.store.get("run", run["id"]))
                        for node in run["nodes"][1:]:
                            try: await control.remote(node, "service.stop", {})
                            except Exception: pass
                finally: await control.close()
            # Shutdown receipt acknowledges the request, not OS process exit.
            # Wait for this fixture's inherited log handle before deleting its
            # directory on Windows; never terminate an application by PID.
            log = fixture.root / "remote-data/scaling/node-service.log"
            for _ in range(100):
                try:
                    if log.exists(): log.rename(log.with_suffix(".closed"))
                    break
                except PermissionError: await asyncio.sleep(.1)
            await fixture.asyncTearDown()
