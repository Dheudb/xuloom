"""Local participation shares the same grouped lifecycle, without SSH or WAN."""
import asyncio
import json
import re
import threading
import time
import unittest
from unittest.mock import patch

from tests import test_scaling_hierarchy as hierarchy
from tests.test_scaling import FakeExecution
from xuloom.scaling import config


class LocalScalingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fx=hierarchy.HierarchyTests();await self.fx.asyncSetUp()
        self.controller=self.fx.controller
        self.original=self.controller.remote
        async def route(node,method,params):
            if node.get("location")=="local":return await self.fx.host.dispatch(method,params)
            return await self.original(node,method,params)
        self.routing=patch.object(self.controller,"remote",side_effect=route);self.routing.start()
        cfg=json.loads(self.fx.config.read_text())
        cfg["model_request_limits"]={"concurrency":16}
        cfg["scaling_nodes"]=[{"id":"local","location":"local","max_agents":100,"group_width":50,
            "automatic":True,"reserve_percent":25,"api_source":"inherit","backend":"native"}]
        self.fx.config.write_text(json.dumps(cfg),encoding="utf-8")
        self.fx.host.ramp=128

    async def asyncTearDown(self):
        self.routing.stop();await self.fx.asyncTearDown()

    async def start_run(self,target=100):
        return await self.controller.dispatch("create",{"goal":"Explore complementary evidence","nodes":["local"],
            "target":target,"accept_high_usage":True})

    async def settle(self,run,seconds=100):
        end=time.monotonic()+seconds
        while time.monotonic()<end:
            row=await self.fx.parent_tick(run)
            if row["status"] in {"completed","blocked","cancelled"}:return row
        self.fail("Local swarm did not settle: "+str(row))

    async def test_hundred_local_workers_have_two_leaders_and_one_global_primary(self):
        with patch("xuloom.scaling.control.resolve",side_effect=AssertionError("Local execution must not resolve SSH")):
            run=await self.start_run()
        final=await self.settle(run)
        self.assertEqual(final["status"],"completed",final)
        leaders=[r for r in self.controller.store.walk("task",run["id"]) if r["kind"]=="leader"]
        self.assertEqual(len(leaders),2)
        self.assertEqual([r["count"] for r in leaders],[50,50])
        workers=[r for r in self.fx.host.store.walk("execution") if r["kind"]=="explore"]
        self.assertEqual(len(workers),100)
        self.assertTrue(all(r["status"]=="completed" for r in workers))
        sessions=[]
        for leader in leaders:
            rows=await self.controller.dispatch("children",{"id":run["id"],"task":leader["id"]})
            self.assertEqual(len([r for r in rows if r["kind"]=="explore"]),50)
            coordinator=[r for r in rows if r["kind"] in {"expand","group","synthesize"}]
            self.assertEqual(len({r["session_id"] for r in coordinator}),1)
            sessions.append(coordinator[0]["session_id"])
        self.assertEqual(len(set(sessions)),2)
        root_turns=[r for r in self.controller.store.walk("task",run["id"]) if r["kind"] in {"plan","synthesize"}]
        self.assertEqual(len({r["session_id"] for r in root_turns}),1)
        self.assertNotIn("hierarchy-private-key",str(await self.controller.dispatch("status",{"id":run["id"]})))

    async def test_local_pause_cancel_and_owner_direction_keep_same_lifecycle(self):
        FakeExecution.hold=asyncio.Event()
        run=await self.start_run(4)
        await self.fx.accepted_leader(run)
        posted=await self.controller.dispatch("message",{"id":run["id"],"request_id":"steer","text":"change toward independent checks"})
        result=await self.fx.settled_message(run,posted["id"])
        self.assertEqual(result["status"],"applied",result)
        self.assertEqual(self.controller.store.get("run",run["id"])["round"],2)
        await self.controller.dispatch("pause",{"id":run["id"]})
        await self.fx.parent_tick(run)
        self.assertEqual(self.controller.store.get("run",run["id"])["status"],"paused")
        await self.controller.dispatch("cancel",{"id":run["id"]})
        self.assertEqual((await self.settle(run))["status"],"cancelled")

    async def test_local_config_does_not_require_ssh_and_rejects_duplicate_capacity(self):
        row=config.validate({"id":"local","location":"local","max_agents":100})
        self.assertIsNone(row["ssh"])
        config.save([row])
        self.assertEqual(config.nodes()[0]["location"],"local")
        with self.assertRaisesRegex(ValueError,"same SSH target"):
            config.save([row,{**row,"id":"alias"}])
        with self.assertRaisesRegex(ValueError,"local model"):
            config.validate({**row,"api_source":"remote"})
        with self.assertRaisesRegex(ValueError,"exceeds"):
            await self.start_run(101)

    async def test_group_counts_are_balanced_at_fifty_boundaries(self):
        from xuloom.scaling.distributed import distribute
        run=await self.start_run(1)
        original=self.controller.store.get("run",run["id"])
        for capacity in (1,50,51,60,99,100,101,1000):
            node={**original["nodes"][1],"max_agents":capacity,"group_width":50}
            current={**original,"id":"balanced-"+str(capacity),"nodes":[original["nodes"][0],node],"target":capacity}
            with self.controller.store.connection() as db:
                distribute(self.controller,current,[{"label":"Evidence","prompt":"Explore alternatives"}],db)
            rows=list(self.controller.store.walk("task",current["id"]))
            sizes=sorted(row["count"] for row in rows)
            self.assertEqual(len(sizes),(capacity+49)//50)
            self.assertEqual(sum(sizes),capacity)
            self.assertLessEqual(max(sizes),50);self.assertLessEqual(max(sizes)-min(sizes),1)
            if capacity==51:self.assertEqual(sizes,[25,26])
            if capacity==60:self.assertEqual(sizes,[30,30])

    async def test_old_runtime_is_rejected_before_accepting_grouped_work(self):
        async def old(node,method,params):
            if method=="check":
                result=await self.fx.host.dispatch(method,params)
                result.pop("capabilities",None);return result
            self.fail("Incompatible runtime must not accept work")
        with patch.object(self.controller,"remote",side_effect=old):
            with self.assertRaisesRegex(ValueError,"v0.1.30"):
                await self.start_run(1)


class LocalDaemonTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_local_service_without_ssh_runs_two_independent_group_sessions(self):
        from tests.test_runtime_connections import RuntimeConnectionTests
        from xuloom.scaling.control import Controller
        from xuloom.scaling.node import Node
        from xuloom.scaling.store import Store
        from tests.test_scaling import resources
        fixture=RuntimeConnectionTests();await fixture.asyncSetUp()
        path=fixture.root/"config/config.json";path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps({"model_connections":[{"id":"fixture","provider":"openai","model":"local-model",
            "models":["local-model"],"api_key":"local-fixture-key","base_url":fixture.base+"/local/v1"}],
            "model_request_limits":{"concurrency":8},"scaling_nodes":[{"id":"local","location":"local","max_agents":4,
                "group_width":2,"automatic":True,"reserve_percent":10,"backend":"native"}]}),encoding="utf-8")
        overlap=threading.Barrier(4);parallel=[]
        def respond(handler,body):
            text=[m.get("content","") for m in body["messages"] if m["role"]=="user"][-1]
            if isinstance(text,list):text="\n".join(b.get("text","") for b in text)
            if "SCALING PLAN" in text:answer={"groups":[{"label":"Approach A","prompt":"First evidence"},{"label":"Approach B","prompt":"Independent alternative evidence"}]}
            elif "SCALING EXPAND" in text:
                count=int(re.search(r"Create up to (\d+)",text)[1])
                answer={"tasks":[{"label":str(i),"prompt":"Independent evidence branch "+str(i)+" partition "+str(__import__("hashlib").sha256(text.encode()).hexdigest()[:12])} for i in range(count)]}
            elif "SCALING SYNTHESIZE" in text or "SCALING REVIEW" in text:answer={"decision":"complete","report":"Evidence reviewed","groups":[]}
            else:
                if text.startswith("Independent evidence branch"):
                    # Cold daemon startup may overlap other full-suite processes.
                    # Still withhold every reply until all four workers arrive:
                    # a serialized executor breaks the barrier and fails below.
                    try:overlap.wait(timeout=30);parallel.append(True)
                    except threading.BrokenBarrierError:parallel.append(False)
                answer="Fixture evidence or summary"
            value=json.dumps(answer) if isinstance(answer,dict) else answer
            handler.send_response(200);handler.send_header("Content-Type","text/event-stream");handler.end_headers()
            frames=[{"choices":[{"index":0,"delta":{"role":"assistant","content":value},"finish_reason":None}]},
                {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}]
            handler.wfile.write(("".join("data: "+json.dumps(f)+"\n\n" for f in frames)+"data: [DONE]\n\n").encode())
            return True
        fixture.response_handler=respond
        control=Controller(Store(fixture.root/"control.sqlite3"),Node(Store(fixture.root/"primary.sqlite3"),resources=resources),hierarchical=True)
        run=None
        try:
            with patch("xuloom.core.config.CONFIG_FILE",path):
                await control.start()
                with patch("xuloom.scaling.control.SSHClient",side_effect=AssertionError("Local nodes do not use SSH")):
                    run=await control.dispatch("create",{"goal":"Compare evidence","nodes":["local"],"accept_high_usage":True})
                end=time.monotonic()+80
                while time.monotonic()<end:
                    final=await control.dispatch("status",{"id":run["id"]})
                    if final["status"] in {"completed","blocked"}:break
                    await asyncio.sleep(.15)
                self.assertEqual(final["status"],"completed",final)
                leaders=[r for r in control.store.walk("task",run["id"]) if r["kind"]=="leader"]
                self.assertEqual(len(leaders),2)
                histories=[]
                for leader in leaders:
                    children=await control.dispatch("children",{"id":run["id"],"task":leader["id"]})
                    self.assertEqual(len([r for r in children if r["kind"]=="explore"]),2)
                    histories.append({r["session_id"] for r in children if r["kind"] in {"expand","group","synthesize"}})
                self.assertTrue(all(len(ids)==1 for ids in histories))
                self.assertTrue(histories[0].isdisjoint(histories[1]))
                self.assertTrue(fixture.records)
                self.assertTrue(any(t.get("function",{}).get("name")=="scaling_shared" for r in fixture.records for t in r["body"].get("tools",[])))
                self.assertEqual(parallel,[True]*4,"Separate group allocations must permit all four workers before any reply")
                self.assertTrue(all(r["headers"]["authorization"]=="Bearer local-fixture-key" for r in fixture.records))
        finally:
            if run:
                current=control.store.get("run",run["id"])
                if current["status"] not in {"completed","cancelled"}:
                    await control.dispatch("cancel",{"id":run["id"]})
                    for _ in range(100):
                        await control.tick_guard(control.store.get("run",run["id"]))
                        if control.store.get("run",run["id"])["status"]=="cancelled":break
                        await asyncio.sleep(.1)
                await control.remote(current["nodes"][1],"service.stop",{})
            await control.close()
            log=fixture.root/"data/scaling/node-service.log"
            for _ in range(100):
                try:
                    if log.exists():log.rename(log.with_suffix(".closed"))
                    break
                except PermissionError:await asyncio.sleep(.1)
            await fixture.asyncTearDown()
