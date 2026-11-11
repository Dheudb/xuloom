"""Deterministic swarm failures and local model/SSH protocol; no WAN inference."""
import asyncio
import copy
import json
import os
from pathlib import Path
import re
import tempfile
import time
import unittest
from unittest.mock import patch

from xuloom.scaling.store import Store
from xuloom.scaling.node import Node, digest
from xuloom.scaling.control import Controller
from xuloom.scaling.config import validate


def resources(_=25):
    return {"suggested": 50000, "pressure": False, "observed": True, "cpus": 64}


def checked(name="host", quota=None):
    return {"protocol": 1, "hostname": name, "machine": digest(name), "resources": resources(),
            "binding": {"connection_id": "fixture", "model": "fixture-model"}, "quota": quota or digest("provider"),
            "limits": {"concurrency": 4, "requests_per_minute": None}, "backend": "native"}


class FakeExecution:
    started, active, peak, hold, fail_kind, invalid, needs_permit = [], 0, 0, None, None, False, False
    def __init__(self, node, row):
        self.node, self.row = node, row
        self.engine = type("Engine", (), {"session_id": row.get("session_id") or row["id"]})()
    async def turn(self, text, *_):
        cls = type(self); cls.started.append(self.row["id"]); cls.active += 1; cls.peak = max(cls.peak, cls.active)
        try:
            if cls.needs_permit:
                reply = await self.node.question(self.row, "model_permit", {"maximum": 64000, "quota": self.row["quota"], "limits": self.row["limits"]})
                self.node.store.update("request", reply["id"], lambda row: row.update(status="released"))
            if self.row["kind"] == "explore" and cls.hold: await cls.hold.wait()
            await asyncio.sleep(.005)
            if cls.fail_kind == self.row["kind"]: return {"ok": False, "persisted": False, "reason": "persistence_error"}
            if self.row["kind"] == "plan":
                result = {"groups": [{"label": "Method A", "prompt": "independent evidence A"}, {"label": "Method B", "prompt": "alternative evidence B"}]}
            elif self.row["kind"] == "expand":
                count = int(re.search(r"Create up to (\d+)", text)[1])
                result = {"tasks": [{"label": str(i), "prompt": self.row["group"] + " independent evidence " + str(i)} for i in range(count)]}
            elif self.row["kind"] == "dialogue":
                owner = text.split("\nOwner message:\n",1)[1].split("\nRuntime reference data",1)[0]
                result = {"decision":"redirect" if owner.startswith("change") else "reply", "reply":"Primary says: "+owner,
                    "goal":"Revised direction: "+owner,"groups":[{"label":"Revised investigation","prompt":"Gather different evidence for "+owner}]}
                if owner == "bad-format": return {"ok":True,"persisted":True,"text":"not JSON","reason":"completed"}
            elif self.row["kind"] == "synthesize": result = {"decision": "complete", "report": "Observed and cited workers", "groups": []}
            else: result = None
            return {"ok": True, "persisted": True, "reason": "completed", "text": "invalid JSON" if cls.invalid else json.dumps(result) if result else "Evidence from " + self.row["id"]}
        finally: cls.active -= 1
    async def close(self): pass


class ScalingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="xuloom-scaling-")
        self.root = Path(self.tmp.name)
        self.environment = patch.dict(os.environ, {"XULOOM_DATA_DIR": str(self.root / "data")}); self.environment.start()
        FakeExecution.started = []; FakeExecution.active = FakeExecution.peak = 0
        FakeExecution.hold = FakeExecution.fail_kind = None; FakeExecution.invalid = FakeExecution.needs_permit = False
        self.node = Node(Store(self.root / "node.sqlite3"), FakeExecution, resources)
        self.node.check = lambda params: checked()
        self.primary = Node(Store(self.root / "primary.sqlite3"), FakeExecution, resources)
        self.primary.check = lambda params: checked("primary")
        owner = self
        class Client:
            def __init__(self, profile, worker=False):
                owner.assertEqual(worker, "scaling"); self.profile = profile
            async def start(self): return {"scaling": 1}
            async def request(self, method, params, **kwargs):
                if owner.disconnected: raise ConnectionError("test SSH disconnected")
                value = await owner.node.dispatch(method.removeprefix("scaling.node."), params)
                if method.endswith("submit") and owner.lose_receipt:
                    owner.lose_receipt = False
                    raise ConnectionError("submit acknowledged remotely, reply lost")
                return value
            async def aclose(self): pass
        self.disconnected = self.lose_receipt = False
        self.controller = Controller(Store(self.root / "control.sqlite3"), self.primary, Client)
        self.saved = [{"id": "lab", "ssh": "lab", "max_agents": 12, "automatic": True, "reserve_percent": 25, "backend": "native"}]
        self.patches = [patch("xuloom.scaling.config.nodes", lambda: copy.deepcopy(self.saved)),
                        patch("xuloom.scaling.control.resolve", lambda key: {"id": key, "host": key})]
        for p in self.patches: p.start()
        await self.node.start(); await self.primary.start()

    async def asyncTearDown(self):
        await self.controller.close(); await self.node.close()
        for p in self.patches: p.stop()
        self.environment.stop(); self.tmp.cleanup()

    async def run_swarm(self, target=12):
        return await self.controller.dispatch("create", {"goal": "Find independent evidence", "nodes": ["lab"], "target": target, "accept_high_usage": True})

    async def drive(self, run, predicate=None, seconds=30):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            row = self.controller.store.get("run", run["id"])
            if predicate and predicate(row): return row
            if not predicate and row["status"] in {"completed", "blocked", "cancelled"}: return row
            await self.controller.tick_guard(row)
            await asyncio.sleep(.03)
        self.fail("Scaling did not settle: " + str(self.controller.view(row)) + " TASKS " + str(list(self.controller.store.walk("task", run["id"]))))

    async def test_real_queue_to_groups_to_workers_to_final_and_no_duplicate_start(self):
        FakeExecution.needs_permit = True
        self.lose_receipt = True
        run = await self.run_swarm(12)
        final = await self.drive(run)
        self.assertEqual(final["status"], "completed", final)
        tasks = list(self.controller.store.walk("task", run["id"]))
        self.assertEqual(len([t for t in tasks if t["kind"] == "explore"]), 12)
        self.assertEqual(len(set(FakeExecution.started)), len(FakeExecution.started))
        self.assertEqual(final["completion_basis"], "model_report")
        self.assertTrue(all(t["receipt"]["persisted"] for t in tasks))
        self.assertTrue(all(t.get("session_id") for t in tasks))

    async def test_no_final_while_workers_wait_and_transport_loss_is_not_interruption(self):
        FakeExecution.hold = asyncio.Event()
        run = await self.run_swarm(6)
        await self.drive(run, lambda _: FakeExecution.active >= 2)
        self.disconnected = True
        await self.controller.tick_guard(self.controller.store.get("run", run["id"]))
        status = await self.controller.dispatch("status", {"id": run["id"]})
        self.assertEqual(status["status"], "running")
        self.assertTrue(any(h["status"] == "unconfirmed" for h in status["health"]))
        self.assertFalse(any(t["kind"] == "synthesize" for t in self.controller.store.walk("task", run["id"])))
        self.disconnected = False; FakeExecution.hold.set()
        self.assertEqual((await self.drive(run))["status"], "completed")

    async def test_pause_stops_dispatch_cancel_reconciles_queue(self):
        run = await self.run_swarm(12)
        await self.controller.dispatch("pause", {"id": run["id"]})
        await self.controller.tick_guard(self.controller.store.get("run", run["id"]))
        self.assertEqual(FakeExecution.started, [])
        await self.controller.dispatch("cancel", {"id": run["id"]})
        self.assertEqual((await self.drive(run))["status"], "cancelled")

    async def test_cancel_before_first_coroutine_step_settles_record(self):
        row=self.node.store.put("execution",{"id":"not-started","run_id":"owned","status":"running",
            "kind":"explore","text":"unused","cwd":str(self.root/"unused")})
        task=self.node.launch(row)
        self.node.cancel_task(row["id"])
        await asyncio.gather(task,return_exceptions=True)
        await asyncio.sleep(0)
        self.assertEqual(self.node.store.get("execution",row["id"])["status"],"cancelled")
        self.assertNotIn(row["id"],self.node.tasks)
        self.assertNotIn(row["id"],FakeExecution.started)

    async def test_repeated_cancel_cannot_interrupt_executor_cleanup(self):
        started,closing,release=asyncio.Event(),asyncio.Event(),asyncio.Event()
        class CleanupExecution(FakeExecution):
            async def turn(inner,*args):
                started.set();await asyncio.Event().wait()
            async def close(inner):
                closing.set();await release.wait()
        self.node.factory=CleanupExecution
        row=self.node.store.put("execution",{"id":"cleanup","run_id":"owned","status":"running",
            "kind":"explore","text":"unused","cwd":str(self.root/"cleanup")})
        task=self.node.launch(row)
        await asyncio.wait_for(started.wait(),3)
        self.node.cancel_task(row["id"])
        await asyncio.wait_for(closing.wait(),3)
        self.node.cancel_task(row["id"])
        self.assertEqual(self.node.store.get("execution",row["id"])["status"],"running")
        release.set();await asyncio.wait_for(task,3);await asyncio.sleep(0)
        self.assertEqual(self.node.store.get("execution",row["id"])["status"],"cancelled")
        self.assertNotIn(row["id"],self.node.tasks)

    async def test_invalid_plan_repair_is_bounded_and_keeps_original_receipts(self):
        FakeExecution.invalid = True
        run = await self.run_swarm(1)
        final = await self.drive(run)
        self.assertEqual(final["status"], "blocked")
        self.assertEqual(len(FakeExecution.started), 3, list(self.controller.store.walk("task", run["id"])))
        self.assertIn("two repairs", final["error"])

    async def test_persistence_failure_not_success_and_retry_requires_explicit_review(self):
        FakeExecution.fail_kind = "plan"
        run = await self.run_swarm(1)
        final = await self.drive(run)
        self.assertEqual(final["status"], "blocked")
        task = next(self.controller.store.walk("task", run["id"]))
        self.assertEqual(task["status"], "failed")
        with self.assertRaisesRegex(ValueError, "confirm retry"):
            await self.controller.dispatch("retry", {"id": run["id"], "task": task["id"]})
        FakeExecution.fail_kind = None
        await self.controller.dispatch("retry", {"id": run["id"], "task": task["id"], "confirm_side_effects": True})
        self.assertEqual((await self.drive(run))["status"], "completed")

    async def test_capacity_rejects_invalid_config_and_requires_high_usage_ack(self):
        for value in (0, -1, True, 1.5):
            with self.assertRaises(ValueError): validate({"id": "a", "ssh": "a", "max_agents": value}, resolve_hosts=False)
        self.assertEqual(validate({"id": "a", "ssh": "a", "max_agents": 10000}, resolve_hosts=False)["max_agents"], 10000)
        with self.assertRaisesRegex(ValueError, "high"):
            await self.controller.dispatch("create", {"goal": "test", "nodes": ["lab"]})
        with self.assertRaisesRegex(ValueError, "exceeds"):
            await self.run_swarm(13)

    async def test_run_token_and_frozen_inputs_prevent_adopting_or_replaying_work(self):
        run = await self.run_swarm(1); private = self.controller.store.get("run", run["id"])
        args = {**self.controller.auth(private), "task": {"id": "owned", "kind": "explore", "text": "first", "label": "first", "group": None}}
        first = await self.node.dispatch("submit", args)
        second = await self.node.dispatch("submit", args)
        self.assertEqual(first["id"], second["id"])
        with self.assertRaisesRegex(ValueError, "different input"):
            await self.node.dispatch("submit", {**args, "task": {**args["task"], "text": "changed"}})
        with self.assertRaisesRegex(ValueError, "ownership"):
            await self.node.dispatch("poll", {**self.controller.auth(private), "token": "0" * 64})
        self.assertNotIn("token", await self.controller.dispatch("status", {"id": run["id"]}))

    async def test_node_pressure_pauses_new_admission_and_limit_is_real(self):
        run = await self.run_swarm(3); private = self.controller.store.get("run", run["id"])
        self.node.resources = lambda _: {**resources(), "suggested": 0, "pressure": True}
        for i in range(5):
            await self.node.dispatch("submit", {**self.controller.auth(private), "task": {"id": "pressure" + str(i), "kind": "explore", "text": "wait", "label": "wait", "group": None}})
        await asyncio.sleep(.2)
        self.assertEqual(self.node.tasks, {})
        self.node.resources = lambda _: {**resources(), "suggested": 2}
        FakeExecution.hold = asyncio.Event()
        await asyncio.sleep(1.2)
        self.assertLessEqual(len(self.node.tasks), 2)
        FakeExecution.hold.set()

    async def test_persisted_grants_survive_controller_restart_and_block_other_node(self):
        run = await self.run_swarm(2); private = self.controller.store.get("run", run["id"]); node = private["nodes"][1]
        request = {"id": "grant", "run_id": run["id"], "task_id": "task", "kind": "model_permit", "status": "pending",
                   "payload": {"quota": node["check"]["quota"], "limits": {"concurrency": 1, "requests_per_minute": None}}}
        self.node.store.put("request", request)
        await self.controller.requests(private, node, [request])
        other = {**request, "id": "other"}; self.node.store.put("request", other)
        replacement = Controller(self.controller.store, self.primary, self.controller.client_factory)
        try:
            await replacement.requests(private, node, [other])
            self.assertEqual(self.node.store.get("request", "other")["status"], "pending")
            self.node.store.update("request", request["id"], lambda row: row.update(status="released"))
            await replacement.requests(private, node, [{**request, "status": "released"}])
            await replacement.requests(private, node, [other])
            self.assertEqual(self.node.store.get("request", "other")["status"], "granted")
        finally:
            await asyncio.gather(*(c.aclose() for c in replacement.clients.values()))

    async def test_ten_thousand_queued_descriptions_do_not_create_processes_or_waiters(self):
        with self.controller.store.connection() as db:
            for i in range(10000):
                Store.put_db(db, "task", {"id": "queued%05d" % i, "run_id": "large", "status": "queued", "text": "logical work"})
        self.assertEqual(self.controller.store.counts("task", "large"), {"queued": 10000})
        self.assertEqual(len(list(self.controller.store.walk("task", "large"))), 10000)
        self.assertEqual(self.node.tasks, {})
        with self.assertRaisesRegex(ValueError, "already claimed"):
            self.controller.store.claim("queued00001", "lab"); self.controller.store.claim("queued00001", "lab")

    async def test_node_restart_marks_running_interrupted_never_replays_it(self):
        self.node.store.put("execution", {"id": "crashed", "run_id": "old", "status": "running"})
        replacement = Node(self.node.store, FakeExecution, resources)
        await replacement.start()
        try:
            self.assertEqual(replacement.store.get("execution", "crashed")["status"], "interrupted")
            self.assertEqual(FakeExecution.started, [])
        finally: await replacement.close()

    async def test_same_physical_host_alias_is_rejected(self):
        self.saved.append({**self.saved[0], "id": "other", "ssh": "other"})
        with self.assertRaisesRegex(ValueError, "same execution host"):
            await self.controller.dispatch("create", {"goal": "test", "nodes": ["lab", "other"], "accept_high_usage": True})

    async def test_start_identity_reconciles_lost_reply_instead_of_spawning_twice(self):
        params = {"goal": "test", "nodes": ["lab"], "accept_high_usage": True, "request_id": "stable-intent"}
        first, second = await asyncio.gather(self.controller.dispatch("create", params), self.controller.dispatch("create", params))
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(list(self.controller.store.walk("run"))), 1)
        with self.assertRaisesRegex(ValueError, "different input"):
            await self.controller.dispatch("create", {**params, "goal": "changed"})

    async def test_full_evidence_pages_and_ownership_are_enforced(self):
        from xuloom.scaling.evidence import page
        self.controller.store.put("task", {"id": "evidence", "run_id": "run", "status": "completed", "receipt": {"text": "a" * 40000 + "END"}})
        first = page(self.controller.store, "run", {"task_id": "evidence", "limit": 30000})
        self.assertFalse(first["complete"])
        last = page(self.controller.store, "run", {"task_id": "evidence", "offset": first["next_offset"]})
        self.assertTrue(last["complete"]); self.assertTrue(last["text"].endswith("END"))
        with self.assertRaisesRegex(ValueError, "another"):
            page(self.controller.store, "other", {"task_id": "evidence"})

    async def test_metadata_scan_stays_bounded_and_never_overwrites_full_results(self):
        import tracemalloc
        payload = "x" * 128000 + "TAIL"
        with self.controller.store.connection() as db:
            for index in range(32):
                Store.put_db(db, "task", {"id": "result%03d" % index, "run_id": "bounded", "status": "completed", "text": payload, "receipt": {"text": payload}, "kind": "explore"})
        tracemalloc.start()
        try:
            rows = list(self.controller.store.walk("task", "bounded", brief=True))
            _, peak = tracemalloc.get_traced_memory()
        finally: tracemalloc.stop()
        self.assertLess(peak, 2 * 1024 * 1024)
        self.assertTrue(all("text" not in row and "text" not in row["receipt"] for row in rows))
        self.controller.interpret({"id": "bounded"}, rows[0])
        self.assertTrue(self.controller.store.get("task", rows[0]["id"])["receipt"]["text"].endswith("TAIL"))

    async def test_provider_queue_rotates_hosts_instead_of_starving_a_later_host(self):
        run = await self.run_swarm(2); private = self.controller.store.get("run", run["id"])
        node_a = private["nodes"][1]; node_b = {**node_a, "id": "second-host"}
        def request(key):
            return {"id": key, "run_id": run["id"], "task_id": key, "kind": "model_permit", "status": "pending",
                "payload": {"quota": node_a["check"]["quota"], "limits": {"concurrency": 1, "requests_per_minute": None}}}
        first, waiting_a, waiting_b = [request(key) for key in ("first", "waiting-a", "waiting-b")]
        for row in (first, waiting_a, waiting_b): self.node.store.put("request", row)
        await self.controller.requests(private, node_a, [first, waiting_a])
        await self.controller.requests(private, node_b, [waiting_b])
        self.node.store.update("request", "first", lambda row: row.update(status="released"))
        await self.controller.requests(private, node_a, [{**first, "status": "released"}, waiting_a])
        self.assertEqual(self.node.store.get("request", "waiting-a")["status"], "pending")
        await self.controller.requests(private, node_b, [waiting_b])
        self.assertEqual(self.node.store.get("request", "waiting-b")["status"], "granted")

    async def test_thousand_worker_plan_uses_twenty_coordinators_without_fixed_250_boundary(self):
        self.saved[0]["max_agents"] = 1000
        run = await self.run_swarm(1000); private = self.controller.store.get("run", run["id"])
        plan = next(self.controller.store.walk("task", run["id"]))
        plan.update(status="completed", receipt={"text": json.dumps({"groups": [{"label": str(i), "prompt": "direction " + str(i)} for i in range(20)]})})
        self.controller.interpret(private, plan)
        groups = list(self.controller.store.walk("group", run["id"]))
        self.assertEqual(len(groups), 20)
        self.assertEqual(sum(g["remaining"] for g in groups), 1000)
        self.assertEqual({g["remaining"] for g in groups}, {50})
        self.assertEqual(FakeExecution.started, [])

    async def test_continue_opens_new_round_without_losing_new_groups(self):
        run = await self.run_swarm(2)
        private = self.controller.store.get("run", run["id"])
        review = self.controller.add_task(private, "synthesize", "review", node="primary")
        review.update(status="completed", receipt={"text": json.dumps({"decision": "continue", "groups": [{"label": "next", "prompt": "new evidence"}], "report": "Need another round"})})
        self.controller.interpret(private, review)
        current = self.controller.store.get("run", run["id"])
        self.assertEqual(current["round"], 2)
        new = [t for t in self.controller.store.walk("task", run["id"]) if t["kind"] == "expand"]
        self.assertEqual([t["round"] for t in new], [2])

    async def test_permission_answer_is_durable_and_delivered_only_to_owner(self):
        run = await self.run_swarm(1); private = self.controller.store.get("run", run["id"]); node = private["nodes"][1]
        question = {"id": "approval", "run_id": run["id"], "kind": "permission", "status": "pending", "payload": {"summary": "write"}}
        self.node.store.put("request", question)
        await self.controller.requests(private, node, [question])
        pending = await self.controller.dispatch("questions", {"id": run["id"]})
        self.assertEqual(pending[0]["status"], "pending")
        await self.controller.dispatch("answer", {"id": run["id"], "question": "approval", "answer": "deny"})
        await self.controller.requests(private, node, [question])
        self.assertEqual(self.node.store.get("request", "approval")["answer"], "deny")


class NativeScalingTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_engine_turn_uses_frozen_local_connection_and_saves_native_history(self):
        from tests.test_runtime_connections import RuntimeConnectionTests
        fixture = RuntimeConnectionTests(); await fixture.asyncSetUp()
        config_file = fixture.root / "config.json"
        config_file.write_text(json.dumps({"model_connections": [{"id": "fixture", "provider": "openai", "models": ["opaque-model"],
            "model": "opaque-model", "api_key": "fixture-private-key", "base_url": fixture.base + "/scaling/v1"}]}), encoding="utf-8")
        node = Node(Store(fixture.root / "node.sqlite3"), resources=resources)
        try:
            with patch("xuloom.core.config.CONFIG_FILE", config_file):
                await node.start()
                args = {"run_id": "native", "token": "a" * 64}
                await node.dispatch("open", {**args, "settings": {"max_agents": 1, "automatic": True, "reserve_percent": 25, "permission": "ask", "backend": "native"}})
                await node.dispatch("submit", {**args, "task": {"id": "native-task", "kind": "plan", "text": "Say hello", "label": "fixture", "group": None}})
                for _ in range(200):
                    value = await node.dispatch("poll", {**args, "ids": ["native-task"]})
                    for question in value["requests"]:
                        if question["kind"] == "model_permit":
                            await node.dispatch("answer", {**args, "id": question["id"], "answer": {"ok": True, "id": question["id"]}})
                    row = value["tasks"][0]
                    if row["status"] in {"completed", "failed"}: break
                    await asyncio.sleep(.05)
                self.assertEqual(row["status"], "completed", row)
                self.assertTrue(row["receipt"]["persisted"])
                self.assertIn("answer-scaling", row["receipt"]["text"])
                self.assertTrue(row["session_id"])
                self.assertEqual(fixture.records[-1]["headers"]["authorization"], "Bearer fixture-private-key")
                self.assertNotIn("fixture-private-key", json.dumps(value))
        finally:
            await node.close(); await fixture.asyncTearDown()


if __name__ == "__main__": unittest.main()
