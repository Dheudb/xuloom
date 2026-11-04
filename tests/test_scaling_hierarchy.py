"""Node primaries continue without any parent RPC; no external inference."""
import asyncio
import json
import time
import unittest
from unittest.mock import patch

from tests.test_scaling import FakeExecution, resources
from tests import test_runtime_connections as fixture_runtime
from xuloom.scaling.control import Controller
from xuloom.scaling.node import Node
from xuloom.scaling.store import Store


class HierarchyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixture_runtime.RuntimeConnectionTests(); await self.fixture.asyncSetUp()
        self.root = self.fixture.root
        config = {"model_connections": [{"id": "local", "provider": "openai", "model": "model-a", "models": ["model-a", "model-b"],
            "api_key": "hierarchy-private-key", "base_url": self.fixture.base + "/hierarchy/v1"}],
            "ssh_connections": [{"id": "lab", "host": "127.0.0.1", "cwd": str(self.root), "python": "python", "platform": "windows" if __import__("os").name == "nt" else "posix"}],
            "scaling_nodes": [{"id": "lab", "ssh": "lab", "max_agents": 6, "automatic": True, "reserve_percent": 10, "backend": "native"}]}
        self.config = self.root / "config.json"; self.config.write_text(json.dumps(config), encoding="utf-8")
        self.patch = patch("xuloom.core.config.CONFIG_FILE", self.config); self.patch.start()
        FakeExecution.started = []; FakeExecution.active = FakeExecution.peak = 0
        FakeExecution.hold = FakeExecution.fail_kind = None; FakeExecution.invalid = False; FakeExecution.needs_permit = True
        self.host = Node(Store(self.root / "host.sqlite3"), FakeExecution, resources)
        self.primary = Node(Store(self.root / "primary.sqlite3"), FakeExecution, resources)
        self.disconnected = False
        owner = self
        class Client:
            def __init__(self, profile, worker=False): pass
            async def start(self): return {"scaling": 1}
            async def request(self, method, params, **_):
                if owner.disconnected: raise ConnectionError("parent is offline")
                return await owner.host.dispatch(method.removeprefix("scaling.node."), params)
            async def aclose(self): pass
        self.client = Client
        self.controller = Controller(Store(self.root / "controller.sqlite3"), self.primary, Client, hierarchical=True)
        await self.host.start(); await self.primary.start()

    async def asyncTearDown(self):
        await self.controller.close(); await self.host.close()
        self.patch.stop(); await self.fixture.asyncTearDown()

    async def create(self, target=6):
        return await self.controller.dispatch("create", {"goal": "Find independent evidence", "nodes": ["lab"],
            "target": target, "accept_high_usage": True})

    async def parent_tick(self, run):
        await self.controller.tick_guard(self.controller.store.get("run", run["id"]))
        await asyncio.sleep(.05)
        return self.controller.store.get("run", run["id"])

    async def wait(self, condition, seconds=16):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            result = condition()
            if result: return result
            await asyncio.sleep(.05)
        diagnostics = {"host": [(r["kind"], r["status"]) for r in self.host.store.walk("execution", brief=True)], "leaders": [{"status": r["status"], "error": r.get("error"), "tasks": self.host.leadership.store.counts("task", r["id"]), "health": self.host.leadership.store.list("health", r["id"])} for r in self.host.leadership.store.walk("run", brief=True)] if self.host.leadership else []}
        self.fail("Node state did not settle: " + str(diagnostics))

    async def accepted_leader(self, run):
        end = time.monotonic() + 12
        while time.monotonic() < end:
            row = await self.parent_tick(run)
            if row["status"] == "blocked": self.fail(row.get("error"))
            leaders = list(self.host.store.walk("execution", run["id"]))
            if any(r.get("kind") == "leader" for r in leaders): return next(r for r in leaders if r["kind"] == "leader")
        self.fail("Node did not accept its autonomous primary")

    async def test_node_primary_finishes_children_while_parent_is_disconnected(self):
        run = await self.create()
        leader = await self.accepted_leader(run)
        self.disconnected = True
        result = await self.wait(lambda: (r if r["status"] in {"completed", "failed"} else None) if (r := self.host.store.get("execution", leader["id"])) else None)
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["receipt"]["persisted"])
        child_run = self.host.leadership.store.get("run", result["child_run_id"])
        self.assertEqual(child_run["status"], "completed")
        workers = [r for r in self.host.leadership.store.walk("task", child_run["id"]) if r["kind"] == "explore"]
        self.assertEqual(len(workers), 6)
        self.assertTrue(all(r["status"] == "completed" for r in workers))
        # The root's confirmed state has not been guessed from the disconnected host.
        self.assertNotEqual(self.controller.store.get("run", run["id"])["status"], "completed")
        self.disconnected = False
        for _ in range(100):
            final = await self.parent_tick(run)
            if final["status"] in {"completed", "blocked"}: break
        self.assertEqual(final["status"], "completed", final)
        self.assertEqual(len(FakeExecution.started), len(set(FakeExecution.started)))

    async def test_inherited_key_is_protected_and_models_can_be_heterogeneous(self):
        config = json.loads(self.config.read_text())
        config["scaling_nodes"][0].update(api_source="local", api_pool=[{"connection": "local", "model": "model-a"}, {"connection": "local", "model": "model-b"}])
        self.config.write_text(json.dumps(config), encoding="utf-8")
        run = await self.create(); leader = await self.accepted_leader(run)
        result = await self.wait(lambda: (r if r["status"] in {"completed", "failed"} else None) if (r := self.host.store.get("execution", leader["id"])) else None)
        self.assertEqual(result["status"], "completed", result)
        leaves = [r for r in self.host.store.walk("execution") if r.get("kind") == "explore"]
        self.assertEqual({r["binding"]["model"] for r in leaves}, {"model-a", "model-b"})
        self.assertNotIn("hierarchy-private-key", self.host.store.path.read_bytes().decode("utf-8", "ignore"))
        self.assertNotIn("hierarchy-private-key", json.dumps(await self.controller.dispatch("status", {"id": run["id"]})))
        from xuloom.security.credentials import protection_mode
        if protection_mode() == "windows-dpapi":
            for path in (self.root / "secrets").glob("*.json"):
                self.assertNotIn("hierarchy-private-key", path.read_text())
        # One durable allocation is shared by both models on this same key.
        grants = [r for r in self.controller.store.walk("permit", run["id"], statuses=["granted"]) if r.get("allocation")]
        self.assertEqual(sum(r["units"] for r in grants), 3)

    async def test_fifty_is_a_governance_width_and_group_primary_keeps_its_session(self):
        config = json.loads(self.config.read_text()); config["scaling_nodes"][0]["max_agents"] = 51
        self.config.write_text(json.dumps(config), encoding="utf-8")
        # Test governance/session identity independently of cold ramp duration;
        # pressure/ramping are covered by the dedicated node-admission test.
        self.host.ramp = 64
        FakeExecution.hold = asyncio.Event()
        run = await self.create(51); leader = await self.accepted_leader(run)
        end=time.monotonic()+20
        while time.monotonic()<end:
            await self.parent_tick(run)
            leaders=[r for r in self.host.store.walk("execution",run["id"]) if r["kind"]=="leader"]
            if len(leaders)==2 and all(self.host.leadership.store.counts("group",r["child_run_id"]) for r in leaders):break
        self.assertEqual(len(leaders),2)
        self.assertEqual(sorted(r["target"] for r in self.host.leadership.store.walk("run")),[25,26])
        FakeExecution.hold.set()
        for leader in leaders:
            result = await self.wait(lambda: (r if r["status"] in {"completed", "failed"} else None) if (r := self.host.store.get("execution", leader["id"])) else None, 60)
            self.assertEqual(result["status"], "completed", result)
            rows=list(self.host.leadership.store.walk("task",leader["child_run_id"]))
            coordinators=[r for r in rows if r["kind"] in {"expand","group","synthesize"}]
            self.assertEqual(len({r["session_id"] for r in coordinators}),1)
            self.assertFalse(any(r["kind"]=="plan" for r in rows),"group leader directly splits its own workers")

    async def test_cancel_propagates_to_node_coordinators_and_held_children(self):
        FakeExecution.hold = asyncio.Event()
        run = await self.create(); leader = await self.accepted_leader(run)
        await self.wait(lambda: any(r["kind"] == "explore" and r["status"] == "running" for r in self.host.store.walk("execution", brief=True)))
        await self.controller.dispatch("cancel", {"id": run["id"]})
        end = time.monotonic() + 20
        while time.monotonic() < end:
            result = await self.parent_tick(run)
            if result["status"] == "cancelled": break
        diagnostics={"status":result["status"],"tasks":[(r["kind"],r["status"]) for r in self.controller.store.walk("task",run["id"],brief=True)],
            "host":[(r["id"],r["kind"],r["status"]) for r in self.host.store.walk("execution",brief=True)],
            "coroutines":{k:(t.done(),t.cancelled()) for k,t in self.host.tasks.items()},
            "leaders":[(r["status"],self.host.leadership.store.counts("task",r["id"])) for r in self.host.leadership.store.walk("run",brief=True)]}
        self.assertEqual(result["status"], "cancelled", diagnostics)
        self.assertEqual(self.host.store.get("execution", leader["id"])["status"], "cancelled")
        self.assertFalse(self.host.tasks)
        self.assertFalse(list(self.controller.store.walk("permit", run["id"], statuses=["granted"])))

    async def test_missing_leader_records_are_interrupted_without_replaying_work(self):
        isolated = Node(Store(self.root / "missing.sqlite3"), FakeExecution, resources)
        isolated.store.put("execution", {"id":"lost-leader", "run_id":"lost-run", "kind":"leader", "status":"running"})
        await isolated.start()
        try:
            self.assertEqual(isolated.store.get("execution", "lost-leader")["status"], "interrupted")
            self.assertFalse(isolated.tasks)
        finally: await isolated.close()

    async def test_existing_but_empty_leader_index_does_not_leave_a_running_ghost(self):
        isolated = Node(Store(self.root / "empty-index.sqlite3"), FakeExecution, resources)
        Store(self.root / "empty-index-leaders.sqlite3")
        isolated.store.put("execution", {"id":"lost-leader", "run_id":"lost-run", "child_run_id":"missing-run", "kind":"leader", "status":"running"})
        await isolated.start()
        try:
            self.assertEqual(isolated.store.get("execution", "lost-leader")["status"], "interrupted")
            self.assertFalse(isolated.tasks)
        finally: await isolated.close()

    async def test_orphan_coordinator_is_preserved_without_a_repeating_dispatch_loop(self):
        from xuloom.scaling.leadership import Leadership
        isolated = Node(Store(self.root / "orphan.sqlite3"), FakeExecution, resources)
        leader = Leadership(isolated)
        leader.store.put("run", {"id":"orphan-run", "status":"starting", "parent_task":"missing-parent"})
        await leader.start()
        try:
            row = leader.store.get("run", "orphan-run")
            self.assertTrue(row["ownership_missing"])
            self.assertEqual(row["status"], "blocked")
            await asyncio.sleep(.5)
            self.assertFalse(leader.jobs)
        finally: await leader.close()

    async def test_offline_allocation_recovers_lost_saved_envelope_and_retry_reacquires(self):
        from xuloom.scaling.distributed import allocate
        run = await self.create(); leader = await self.accepted_leader(run)
        stored = self.controller.store.get("run", run["id"])
        node = stored["nodes"][1]
        task = self.controller.store.get("task", leader["id"])
        original = json.loads(task["text"]); expected = original.pop("allocations")
        task["text"] = json.dumps(original)
        self.assertEqual(allocate(self.controller, stored, node, task), expected)
        # A new attempt cannot reuse an old node assignment's allocation.
        for permit in self.controller.store.walk("permit", run["id"], statuses=["granted"]):
            if permit.get("task_id") == task["id"]:
                self.controller.store.update("permit", permit["id"], lambda row: row.update(status="released"))
        self.controller.store.update("task", task["id"], lambda row: row.update(status="failed", handled=True))
        fresh = await self.controller.dispatch("retry", {"id":run["id"], "task":task["id"], "confirm_side_effects":True})
        self.assertNotIn("allocations", json.loads(fresh["text"]))
        self.assertTrue(allocate(self.controller, stored, node, fresh))

    async def test_inherit_ignores_saved_pool_until_explicitly_selected(self):
        from xuloom.scaling import models
        inherited = models.export({"api_source":"inherit", "api_pool":[{"connection":"missing"}]})
        self.assertEqual(inherited[0]["binding"]["connection_id"], "local")
        with self.assertRaisesRegex(ValueError, "at least one"):
            models.export({"api_source":"local", "api_pool":[]})

    async def test_explicit_permission_waits_offline_and_returns_through_the_parent(self):
        class ApprovalExecution(FakeExecution):
            async def turn(inner, text, *args):
                if inner.row["kind"] == "explore":
                    choice = await inner.node.question(inner.row, "permission", {"tool":"write", "path":"owned-output"})
                    if choice != "allow": return {"ok":False,"persisted":True,"reason":"permission_denied"}
                return await super().turn(text, *args)
        self.host.factory = ApprovalExecution
        run = await self.create(1); leader = await self.accepted_leader(run)
        self.disconnected = True
        await self.wait(lambda: self.host.leadership.questions(run["id"]))
        self.assertEqual(self.host.store.get("execution", leader["id"])["status"], "running")
        self.disconnected = False
        await self.parent_tick(run)
        questions = await self.controller.dispatch("questions", {"id":run["id"]})
        self.assertEqual(len(questions), 1)
        await self.controller.dispatch("answer", {"id":run["id"], "question":questions[0]["id"], "answer":"allow"})
        end = time.monotonic() + 20
        while time.monotonic() < end:
            final = await self.parent_tick(run)
            if final["status"] in {"completed","blocked"}: break
        self.assertEqual(final["status"], "completed", final)


    async def talk(self,run,text,key="owner-message"):
        return await self.controller.dispatch("message",{"id":run["id"],"text":text,"request_id":key})

    async def settled_message(self,run,key,seconds=30):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            await self.parent_tick(run)
            message=self.controller.store.get("message",key)
            if message["status"] in {"applied","failed","cancelled"}: return message
        self.fail("Owner message did not settle: "+str(message))

    async def test_owner_chat_uses_primary_history_without_restarting_workers(self):
        FakeExecution.hold=asyncio.Event()
        run=await self.create(); await self.accepted_leader(run)
        before=list(self.controller.store.walk("task",run["id"],brief=True))
        message=await self.talk(run,"How is progress?")
        duplicate=await self.talk(run,"How is progress?")
        self.assertEqual(message["id"],duplicate["id"])
        result=await self.settled_message(run,message["id"])
        self.assertEqual(result["status"],"applied",result)
        current=self.controller.store.get("run",run["id"])
        self.assertEqual(current["goal"],run["goal"]);self.assertEqual(current["round"],1)
        primary=[r for r in self.controller.store.walk("task",run["id"]) if r["kind"] in {"plan","dialogue"}]
        self.assertEqual(len({r.get("session_id") for r in primary}),1)
        self.assertFalse(any(r.get("superseded") for r in before))
        self.assertFalse(current.get("conversation_pending"))
        with self.assertRaisesRegex(ValueError,"different text"):
            await self.talk(run,"Other text")

    async def test_direction_waits_for_disconnected_old_workers_before_new_round(self):
        FakeExecution.hold=asyncio.Event()
        run=await self.create(); leader=await self.accepted_leader(run)
        self.disconnected=True
        message=await self.talk(run,"change to independent validation")
        end=time.monotonic()+20
        while time.monotonic()<end:
            await self.parent_tick(run)
            pending=self.controller.store.get("message",message["id"])
            if pending["status"]=="applying" and pending["nodes"]: break
        self.assertEqual(pending["status"],"applying",pending)
        self.assertEqual(pending["nodes"]["lab"]["status"],"pending")
        self.assertEqual(self.controller.store.get("run",run["id"])["round"],1)
        self.disconnected=False
        result=await self.settled_message(run,message["id"])
        self.assertEqual(result["status"],"applied",result)
        current=self.controller.store.get("run",run["id"])
        self.assertEqual(current["round"],2)
        self.assertIn("independent validation",current["goal"])
        self.assertEqual(self.host.store.get("execution",leader["id"])["status"],"cancelled")
        self.assertTrue(self.controller.store.get("task",leader["id"])["superseded"])
        FakeExecution.hold.set()
        for _ in range(150):
            current=await self.parent_tick(run)
            if current["status"] in {"completed","blocked"}:break
        self.assertEqual(current["status"],"completed",current)

    async def test_concurrency_one_yields_a_confirmed_idle_slot_for_owner_chat(self):
        config=json.loads(self.config.read_text());config["model_request_limits"]={"concurrency":1}
        self.config.write_text(json.dumps(config),encoding="utf-8")
        FakeExecution.hold=asyncio.Event()
        run=await self.create(1); await self.accepted_leader(run)
        message=await self.talk(run,"What is happening?")
        result=await self.settled_message(run,message["id"])
        self.assertEqual(result["status"],"applied",result)
        self.assertFalse(list(self.controller.store.walk("permit",run["id"],statuses=["suspended"])))
        self.assertEqual(self.controller.store.get("run",run["id"])["round"],1)

    async def test_paused_runs_accept_questions_without_resuming_exploration(self):
        run=await self.create(1)
        await self.controller.dispatch("pause",{"id":run["id"]})
        message=await self.talk(run,"Please explain the current plan")
        result=await self.settled_message(run,message["id"])
        self.assertEqual(result["status"],"applied",result)
        self.assertEqual(self.controller.store.get("run",run["id"])["status"],"paused")
        self.assertFalse(any(r["kind"]=="leader" for r in self.controller.store.walk("task",run["id"])))

    async def test_failed_chat_keeps_original_direction_and_cancel_does_not_apply_it(self):
        run=await self.create(1)
        message=await self.talk(run,"bad-format")
        result=await self.settled_message(run,message["id"])
        self.assertEqual(result["status"],"failed",result)
        self.assertEqual(len([r for r in self.controller.store.walk("task",run["id"]) if r["kind"]=="dialogue"]),3)
        self.assertEqual(self.controller.store.get("run",run["id"])["goal"],run["goal"])
        fresh=await self.talk(run,"change direction",key="cancelled-message")
        await self.controller.dispatch("cancel",{"id":run["id"]})
        await self.parent_tick(run)
        self.assertEqual(self.controller.store.get("message",fresh["id"])["status"],"cancelled")
        with self.assertRaisesRegex(ValueError,"cancelled"):
            await self.talk(run,"Do more",key="after-cancel")


    async def test_message_finish_failure_rolls_back_reply_slot_and_run_together(self):
        from xuloom.scaling.conversation import finish
        run=await self.create(1)
        message=await self.talk(run,"What is the plan?")
        self.controller.store.put("permit",{"id":"reserved-slot","run_id":run["id"],"status":"suspended"})
        original=self.controller.store.put_db
        def fail_run(db,kind,value):
            if kind=="run": raise OSError("Injected final message save failure")
            return original(db,kind,value)
        with patch.object(self.controller.store,"put_db",side_effect=fail_run):
            with self.assertRaisesRegex(OSError,"save failure"):
                finish(self.controller,run,message,status="applied",reply="Saved answer")
        self.assertEqual(self.controller.store.get("message",message["id"])["status"],"queued")
        self.assertEqual(self.controller.store.get("permit","reserved-slot")["status"],"suspended")
        self.assertTrue(self.controller.store.get("run",run["id"])["conversation_pending"])
        finish(self.controller,run,message,status="applied",reply="Saved answer")
        self.assertEqual(self.controller.store.get("permit","reserved-slot")["status"],"granted")
        self.assertFalse(self.controller.store.get("run",run["id"])["conversation_pending"])

    async def test_durable_pending_message_is_consumed_once_by_replacement_controller(self):
        run=await self.create(1)
        await self.controller.dispatch("pause",{"id":run["id"]})
        message=await self.talk(run,"Explain this paused goal")
        self.controller=Controller(Store(self.root/"controller.sqlite3"),self.primary,self.client,hierarchical=True)
        result=await self.settled_message(run,message["id"])
        self.assertEqual(result["status"],"applied",result)
        self.assertEqual(self.controller.store.get("run",run["id"])["status"],"paused")
        self.assertEqual(len([r for r in self.controller.store.walk("task",run["id"]) if r["kind"]=="dialogue"]),1)
        self.assertEqual(len(await self.controller.dispatch("messages",{"id":run["id"]})),1)

    async def test_slot_yield_checks_hosts_independently_and_never_reclaims_disconnected(self):
        from xuloom.scaling.conversation import yield_slots
        run=await self.create(1)
        run=self.controller.store.get("run",run["id"])
        run["nodes"]=[{"id":key} for key in ("first","second","offline")]
        for node in run["nodes"]:
            self.controller.store.put("health",{"id":run["id"]+"_"+node["id"],"run_id":run["id"],
                "status":"disconnected" if node["id"]=="offline" else "connected"})
            self.controller.store.put("permit",{"id":"slot-"+node["id"],"run_id":run["id"],
                "node":node["id"],"allocation":True,"status":"granted"})
        reached=set();both=asyncio.Event()
        async def idle(node,method,params):
            self.assertNotEqual(node["id"],"offline");self.assertEqual(method,"model.idle")
            reached.add(node["id"])
            if len(reached)==2: both.set()
            await asyncio.wait_for(both.wait(),3)
            return {"idle":True}
        with patch.object(self.controller,"remote",side_effect=idle):
            await yield_slots(self.controller,run)
        self.assertEqual(reached,{"first","second"})
        self.assertEqual(self.controller.store.get("permit","slot-first")["status"],"suspended")
        self.assertEqual(self.controller.store.get("permit","slot-second")["status"],"suspended")
        self.assertEqual(self.controller.store.get("permit","slot-offline")["status"],"granted")
