"""Real SQLite/Node paths; no remote models or external machines."""
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.test_scaling import checked, resources, FakeExecution
from xuloom.scaling.node import Node
from xuloom.scaling.store import Store
from xuloom.scaling import shared


class SharedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix="xuloom-shared-");self.root=Path(self.tmp.name)
        self.env=patch.dict(os.environ,{"XULOOM_DATA_DIR":str(self.root/"data")});self.env.start()
        self.node=Node(Store(self.root/"node.sqlite3"),FakeExecution,resources)
        self.node.check=lambda p:checked()
        self.auth={"run_id":"run-one","token":"a"*64}
        await self.node.dispatch("open",{**self.auth,"settings":{"permission":"full","max_agents":10,"automatic":False,"reserve_percent":25,
            "shared_run":"run-one","shared_node":"host-one"}})

    async def asyncTearDown(self):
        await self.node.close();self.env.stop();self.tmp.cleanup()

    async def submit(self,key,text="same operation",**extra):
        await self.node.dispatch("submit",{**self.auth,"task":{"id":key,"kind":"explore","text":text,**extra}})
        return self.node.store.get("execution",key)

    async def test_atomic_host_claim_across_threads_and_no_ttl_reassignment(self):
        def reserve(i):return shared.claim(self.node,{"id":"actor-"+str(i),"run_id":"run-one","text":"operation + input"})
        with ThreadPoolExecutor(max_workers=12) as pool:results=list(pool.map(reserve,range(24)))
        self.assertEqual(sum(r["accepted"] for r in results),1)
        owner=next(r["claim"]["actor"] for r in results if r["accepted"])
        value=shared.claim(self.node,{"id":owner,"run_id":"run-one","text":"operation + input"})
        self.assertTrue(value["accepted"])
        self.assertEqual(shared.query(self.node.store,"run-one")["total"],1)

    async def test_submit_deduplicates_without_history_and_preserves_verify_and_input_revision(self):
        first=await self.submit("first");duplicate=await self.submit("second")
        self.assertEqual(first["status"],"queued");self.assertEqual(duplicate["status"],"deduplicated")
        self.assertFalse(duplicate["receipt"]["ok"]);self.assertEqual(duplicate["dedup_source"]["actor"],"first")
        self.assertIsNone(duplicate["session_id"]);self.assertFalse(self.node.tasks)
        for key in ("check-one","check-two"):
            self.assertEqual((await self.submit(key,purpose="verify"))["status"],"queued")
        self.assertEqual((await self.submit("new-input",input_version="2"))["status"],"queued")
        replay=await self.node.dispatch("submit",{**self.auth,"task":{"id":"second","kind":"explore","text":"same operation"}})
        self.assertEqual(replay["status"],"deduplicated")

    async def test_stable_work_key_across_different_wording_and_explicit_variants(self):
        await self.submit("one",text="look up doi",work_key="doi:123")
        self.assertEqual((await self.submit("two",text="fetch same DOI",work_key="doi:123"))["status"],"deduplicated")
        self.assertEqual((await self.submit("three",work_key="doi:123",variant="independent-source"))["status"],"queued")

    async def test_sync_replay_monotonic_origin_and_run_boundaries(self):
        row=await self.submit("worker")
        note=shared.operation(self.node,row,{"action":"note","summary":"observed","detail":"full source"})
        remote=Store(self.root/"remote.sqlite3")
        batch=shared.changes(self.node.store,"run-one")["records"]
        shared.import_records(remote,"run-one",batch,origin="host-one");cursor=remote.cursor(shared.board("run-one"))
        shared.import_records(remote,"run-one",batch,origin="host-one")
        self.assertEqual(remote.cursor(shared.board("run-one")),cursor)
        changed={**shared.payload(note),"revision":2,"detail":"new source"}
        shared.import_records(remote,"run-one",[changed,note])
        self.assertEqual(remote.get("shared",note["id"])["detail"],"new source")
        with self.assertRaisesRegex(ValueError,"conflicting"):shared.import_records(remote,"run-one",[{**changed,"detail":"conflict"}])
        with self.assertRaises(ValueError):shared.import_records(remote,"foreign",[note])
        with self.assertRaises(ValueError):shared.import_records(remote,"run-one",[{**changed,"node":"spoof","revision":3}])
        shared.import_records(remote,"run-one",[{**note,"id":"ignored","node":"other"}],origin="host-one")
        self.assertEqual(shared.query(remote,"run-one")["total"],2)
        with self.assertRaises(ValueError):await self.node.dispatch("shared.sync",{**self.auth,"token":"b"*64,"records":[]})

    async def test_release_only_by_owner_and_failed_results_need_review(self):
        row=await self.submit("owner");other=await self.submit("different",text="other")
        entry=shared.query(self.node.store,"run-one",kind="claim",text="owner")["entries"][0]
        with self.assertRaises(ValueError):shared.operation(self.node,other,{"action":"release","entry":entry["id"]})
        shared.finish(self.node,row,{"status":"failed","receipt":{"ok":False,"persisted":True,"text":"API unavailable"}})
        self.assertEqual(self.node.store.get("shared",entry["id"])["status"],"needs_review")
        self.assertEqual((await self.submit("no-blind-replay"))["status"],"deduplicated")
        shared.operation(self.node,row,{"action":"release","entry":entry["id"]})
        self.assertEqual((await self.submit("reviewed-retry"))["status"],"queued")
        notes=shared.query(self.node.store,"run-one",kind="note")["entries"]
        self.assertEqual(notes[0]["category"],"failure");self.assertEqual(notes[0]["verification"],"reported")

    async def test_owned_artifacts_lossless_pages_hashes_and_changed_files(self):
        row=await self.submit("writer");root=Path(row["cwd"]);root.mkdir(parents=True)
        content=("研究结果🐦\n"*5000).encode();(root/"evidence.txt").write_bytes(content)
        entry=shared.operation(self.node,row,{"action":"artifact","path":"evidence.txt","summary":"evidence"})
        result=b"";offset=0
        while True:
            page=shared.read_artifact(self.node,"run-one",entry["id"],offset)
            result+=base64.b64decode(page["base64"])
            if page["next_offset"] is None:break
            offset=page["next_offset"]
        self.assertEqual(result,content);self.assertEqual(entry["verification"],"bytes_hashed")
        with self.assertRaises(ValueError):shared.read_artifact(self.node,"foreign",entry["id"])
        (root/"evidence.txt").write_text("changed",encoding="utf-8")
        with self.assertRaisesRegex(ValueError,"changed"):shared.read_artifact(self.node,"run-one",entry["id"])
        (root.parent/"outside.txt").write_text("private")
        with self.assertRaises(ValueError):shared.file_info(self.node,row,"../outside.txt")
        with self.assertRaises(ValueError):shared.file_info(self.node,row,str(root.parent/"outside.txt"))
        try:(root/"link").symlink_to(root.parent/"outside.txt")
        except OSError:pass
        else:
            with self.assertRaises(ValueError):shared.file_info(self.node,row,"link")

    async def test_incremental_context_is_bounded_without_losing_unread_notes(self):
        row=await self.submit("reader");writer=await self.submit("writer",text="different")
        cursor=self.node.store.cursor(shared.board("run-one"));ids=[]
        for i in range(19):ids.append(shared.operation(self.node,writer,{"action":"note","summary":"finding "+str(i)})["id"])
        found=[]
        for _ in range(3):
            page=shared.context(self.node,row,cursor);cursor=page["cursor"]
            self.assertLessEqual(len(page["entries"]),8);found.extend(r["id"] for r in page["entries"])
        self.assertEqual(found,ids)
        self.assertFalse(shared.context(self.node,row,cursor)["entries"])
        self.assertTrue(all(r["verification"]=="reported" for r in page["entries"]))

    async def test_api_failure_stops_and_persists_without_ssh_liveness_guessing(self):
        class Offline:
            def __init__(self,*_):pass
            async def turn(self,*_):raise ConnectionError("Provider retry policy exhausted: network unavailable")
            async def close(self):pass
        self.node.factory=Offline
        row=await self.submit("offline")
        self.node.store.update("execution",row["id"],lambda r:r.update(status="running"))
        await self.node.execute(row)
        final=self.node.store.get("execution",row["id"])
        self.assertEqual(final["status"],"failed");self.assertIn("network unavailable",final["error"])
        self.assertFalse(self.node.tasks);self.assertFalse(final.get("shared_error"))
        self.assertEqual(shared.query(self.node.store,"run-one",kind="claim")["entries"][0]["status"],"needs_review")
        self.assertEqual(shared.query(self.node.store,"run-one",kind="note")["entries"][0]["category"],"failure")

    async def test_foreign_read_and_remote_artifact_validation_do_not_become_model_requests(self):
        row=await self.submit("reader");note=shared.operation(self.node,row,{"action":"note","summary":"source"})
        wrong={**row,"run_id":"foreign-job"}
        self.node.store.put("job",{"id":"foreign-job","settings":{"shared_run":"foreign-run"}})
        with self.assertRaises(ValueError):shared.operation(self.node,wrong,{"action":"read","entry":note["id"]})
        with self.assertRaises(ValueError):await shared.SharedTool(self.node,row).execute(action="read_artifact",entry=note["id"])
        self.assertFalse(list(self.node.store.walk("request")))

    async def test_partition_conflicts_are_preserved_and_cached_claim_stops_new_duplicate(self):
        first=await self.submit("first")
        peer=Node(Store(self.root/"peer.sqlite3"),FakeExecution,resources)
        peer.check=lambda p:checked("peer")
        await peer.dispatch("open",{**self.auth,"settings":{"permission":"full","max_agents":10,"automatic":False,"reserve_percent":25,
            "shared_run":"run-one","shared_node":"host-two"}})
        await peer.dispatch("submit",{**self.auth,"task":{"id":"offline-peer","kind":"explore","text":first["text"]}})
        incoming=shared.changes(peer.store,"run-one")["records"]
        shared.import_records(self.node.store,"run-one",incoming,origin="host-two")
        entries=shared.query(self.node.store,"run-one",kind="claim")["entries"]
        self.assertEqual(len(entries),2);self.assertTrue(all(r["conflict"] for r in entries))
        self.assertEqual(self.node.store.get("execution","first")["status"],"queued")
        self.assertEqual((await self.submit("later"))["status"],"deduplicated")

    async def test_cross_host_artifact_read_uses_authenticated_owner_routing(self):
        from xuloom.scaling.control import Controller
        source=await self.submit("writer");root=Path(source["cwd"]);root.mkdir(parents=True)
        (root/"report.md").write_text("peer evidence",encoding="utf-8")
        entry=shared.operation(self.node,source,{"action":"artifact","path":"report.md","summary":"report"})
        peer=Node(Store(self.root/"peer.sqlite3"),FakeExecution,resources);peer.check=lambda p:checked("peer")
        await peer.dispatch("open",{**self.auth,"settings":{"permission":"full","max_agents":10,"automatic":False,"reserve_percent":25,
            "shared_run":"run-one","shared_node":"host-two"}})
        await peer.dispatch("submit",{**self.auth,"task":{"id":"reader","kind":"explore","text":"independent"}})
        shared.import_records(peer.store,"run-one",shared.changes(self.node.store,"run-one")["records"])
        store=Store(self.root/"root.sqlite3");shared.import_records(store,"run-one",shared.changes(self.node.store,"run-one")["records"])
        controller=Controller(store,local=self.node)
        async def remote(node,method,params):return await (self.node if node["id"]=="host-one" else peer).dispatch(method,params)
        controller.remote=remote
        run={"id":"run-one","token":self.auth["token"],"nodes":[{"id":"host-one"},{"id":"host-two"}]}
        task=asyncio.create_task(shared.SharedTool(peer,peer.store.get("execution","reader")).execute(action="read_artifact",entry=entry["id"]))
        try:
            await asyncio.sleep(0)
            await controller.requests(run,{"id":"host-two"},peer.store.list("request",parent="run-one"))
            result=json.loads(await asyncio.wait_for(task,2))
            self.assertEqual(result["text"],"peer evidence");self.assertEqual(result["sha256"],entry["sha256"])
            self.assertFalse(list(peer.store.walk("request",statuses=["pending"])))
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True);await peer.close()
