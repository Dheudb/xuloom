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

    async def test_reviewed_retry_keeps_claim_identity_and_releases_only_failed_owner(self):
        fields = dict(work_key="check-data", purpose="verify", variant="independent", input_version="v7")
        old = await self.submit("original", **fields)
        with self.assertRaisesRegex(ValueError, "failed/interrupted"):
            await self.submit("premature", retry_of="original", **fields)
        self.node.store.update("execution", "original", lambda row: row.update(status="failed"))
        shared.settle(self.node, old, {"status":"failed"})
        with self.assertRaisesRegex(ValueError, "work identity"):
            await self.submit("wrong", retry_of="original", **{**fields,"input_version":"v8"})
        new = await self.submit("retry", text="review previous effects, then retry", retry_of="original", **fields)
        self.assertEqual(new["status"], "queued")
        self.assertEqual({k:new[k] for k in fields}, fields)
        claims = shared.query(self.node.store,"run-one",kind="claim")["entries"]
        self.assertEqual(len({c["work"] for c in claims}), 1)
        self.assertEqual({c["status"] for c in claims}, {"held","released"})
        self.assertEqual(self.node.store.get("execution","original")["retried_by"],"retry")
        with self.assertRaisesRegex(ValueError,"unretried"):
            await self.submit("duplicate-retry",retry_of="original",**fields)

    async def test_retry_without_explicit_work_key_retains_original_claim(self):
        old = await self.submit("original", purpose="verify")
        self.node.store.update("execution","original",lambda row:row.update(status="interrupted"))
        shared.settle(self.node,old,{"status":"interrupted"})
        new = await self.submit("retry", text="changed retry guidance", purpose="verify", retry_of="original")
        self.assertEqual(new["status"],"queued")
        self.assertEqual(len({r["work"] for r in shared.query(self.node.store,"run-one",kind="claim")["entries"]}),1)

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
        self.assertEqual(base64.b64decode(shared.read_artifact(self.node,"run-one",entry["id"])["base64"]),content[:24000])
        new=shared.operation(self.node,row,{"action":"artifact","path":"evidence.txt","summary":"evidence"})
        self.assertNotEqual(new["id"],entry["id"])
        self.assertEqual(shared.read_artifact(self.node,"run-one",new["id"])["text"],"changed")
        (root.parent/"outside.txt").write_text("private")
        with self.assertRaises(ValueError):shared.file_info(self.node,row,"../outside.txt")
        with self.assertRaises(ValueError):shared.file_info(self.node,row,str(root.parent/"outside.txt"))
        try:(root/"link").symlink_to(root.parent/"outside.txt")
        except OSError:pass
        else:
            with self.assertRaises(ValueError):shared.file_info(self.node,row,"link")

    async def test_snapshot_detects_corrupt_pages_even_with_original_mtime_and_reopens_index(self):
        from xuloom.scaling import artifacts
        row=await self.submit("writer");root=Path(row["cwd"]);root.mkdir(parents=True)
        (root/"data.bin").write_bytes(b"a"*50000)
        entry=shared.operation(self.node,row,{"action":"artifact","path":"data.bin","summary":"binary"})
        blob,index=artifacts.paths(self.node,"run-one",entry["sha256"]);stamp=blob.stat()
        with blob.open("r+b") as handle:handle.seek(24010);handle.write(b"b")
        os.utime(blob,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
        self.assertEqual(len(base64.b64decode(shared.read_artifact(self.node,"run-one",entry["id"])["base64"])),24000)
        with self.assertRaisesRegex(ValueError,"snapshot changed"):shared.read_artifact(self.node,"run-one",entry["id"],24000)
        index.write_bytes(b"{}")
        artifacts.load_index.cache_clear()
        with self.assertRaisesRegex(ValueError,"index changed"):shared.read_artifact(self.node,"run-one",entry["id"])

    async def test_legacy_manifest_archives_once_and_retains_exact_bytes(self):
        from xuloom.scaling import artifacts
        row=await self.submit("legacy");root=Path(row["cwd"]);root.mkdir(parents=True)
        (root/"old.txt").write_bytes(b"legacy"*10000)
        info=shared.file_info(self.node,row,"old.txt")
        entry=shared.put(self.node.store,{**shared.owned(self.node,row),**info,"id":"legacy-artifact","kind":"artifact","status":"reported","revision":1})
        with patch.object(artifacts,"file_info",wraps=artifacts.file_info) as inspect:
            first=shared.read_artifact(self.node,"run-one",entry["id"])
            (root/"old.txt").unlink()
            second=shared.read_artifact(self.node,"run-one",entry["id"],24000)
            self.assertEqual(inspect.call_count,1)
        self.assertEqual(first["sha256"],second["sha256"])
        self.assertEqual(len(base64.b64decode(second["base64"])),24000)

    async def test_snapshot_checks_publication_bytes_empty_files_and_unaligned_pages(self):
        from xuloom.scaling import artifacts
        row=await self.submit("writer");root=Path(row["cwd"]);root.mkdir(parents=True)
        content=b"abcdefgh"*10000;(root/"source.bin").write_bytes(content)
        original=artifacts.atomic_copy
        def corrupt(target,*args):
            original(target,*args)
            with target.open("r+b") as handle:handle.write(b"CORRUPT")
        with patch.object(artifacts,"atomic_copy",side_effect=corrupt):
            with self.assertRaisesRegex(ValueError,"during publication"):
                shared.operation(self.node,row,{"action":"artifact","path":"source.bin","summary":"corrupt"})
        entry=shared.operation(self.node,row,{"action":"artifact","path":"source.bin","summary":"good"})
        page=shared.read_artifact(self.node,"run-one",entry["id"],23990)
        self.assertEqual(base64.b64decode(page["base64"]),content[23990:47990])
        with self.assertRaises(ValueError):shared.read_artifact(self.node,"run-one",entry["id"],True)
        (root/"empty.txt").touch()
        empty=shared.operation(self.node,row,{"action":"artifact","path":"empty.txt","summary":"empty"})
        self.assertEqual(shared.read_artifact(self.node,"run-one",empty["id"])["base64"],"")
        self.assertIsNone(shared.read_artifact(self.node,"run-one",empty["id"])["next_offset"])

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

    async def test_initial_context_and_cursor_share_one_snapshot(self):
        reader=await self.submit("reader");writer=await self.submit("writer",text="other")
        shared.operation(self.node,writer,{"action":"note","summary":"before snapshot"})
        query=shared.query;published=[]
        def concurrent(*args,**kwargs):
            result=query(*args,**kwargs)
            if not published:published.append(shared.operation(self.node,writer,{"action":"note","summary":"concurrent finding"})["id"])
            return result
        with patch.object(shared,"query",side_effect=concurrent):first=shared.context(self.node,reader)
        following=shared.context(self.node,reader,first["cursor"])
        delivered=[entry["id"] for page in (first,following) for entry in page["entries"]]
        self.assertIn(published[0],delivered,"A committed finding must appear in the initial snapshot or a later event")

    async def test_artifact_paging_does_not_rescan_the_entire_file(self):
        from xuloom.core.storage import IOPath
        row=await self.submit("reader");root=Path(row["cwd"]);root.mkdir(parents=True)
        content=b"evidence"*(1024*128);(root/"large.bin").write_bytes(content)
        entry=shared.operation(self.node,row,{"action":"artifact","path":"large.bin","summary":"large"})
        real_open=IOPath.open;read_bytes=[0]
        class Reader:
            def __init__(self,handle):self.handle=handle
            def __enter__(self):return self
            def __exit__(self,*args):return self.handle.__exit__(*args)
            def __getattr__(self,key):return getattr(self.handle,key)
            def read(self,*args):
                part=self.handle.read(*args);read_bytes[0]+=len(part);return part
        def counted(path,*args,**kwargs):
            handle=real_open(path,*args,**kwargs)
            return Reader(handle) if args and args[0]=="rb" and not str(path).endswith(".json") else handle
        offset=0;result=b""
        with patch.object(IOPath,"open",counted):
            while True:
                page=shared.read_artifact(self.node,"run-one",entry["id"],offset)
                result+=base64.b64decode(page["base64"])
                if page["next_offset"] is None:break
                offset=page["next_offset"]
        self.assertEqual(result,content)
        self.assertLessEqual(read_bytes[0],len(content)+48000,"Page reads must scale with served data, not page count times file size")

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
