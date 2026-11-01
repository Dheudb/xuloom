import asyncio
import base64
import hashlib
import io
import os
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

from PIL import Image
from xuloom.core.session import SessionManager
from xuloom.core.session_lock import SessionBusyError
from xuloom.desktop.bridge import DesktopBridge
from xuloom.desktop.attachments import attachment_directory, prepare_attachments
from xuloom.remote import attachments
from xuloom.remote.ssh import RemoteDisconnected


class Client:
    def __init__(self, bridge): self.bridge, self.info, self.calls = bridge, {"attachments": 1}, []
    async def request(self, method, params, **kwargs):
        self.calls.append((method, params))
        return await self.bridge.dispatch(method, params)


class AttachmentTransferTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xuloom-upload-"); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.work = self.root / "work"; self.work.mkdir()
        self.env = patch.dict(os.environ, {"XULOOM_DATA_DIR": str(self.root / "data"), "XULOOM_CONFIG_DIR": str(self.root / "config"), "HOME": str(self.root / "home"), "USERPROFILE": str(self.root / "home"), "XULOOM_SKILLS_PATHS": ""})
        self.env.start(); self.addCleanup(self.env.stop)
        self.sid = str(uuid.uuid4())
        with SessionManager(cwd=str(self.work)) as manager: manager.save_session_state(self.sid, [], {})
        self.bridge = DesktopBridge(lambda event: None, self.work); self.bridge.resume_id = self.sid
        self.addAsyncCleanup(self.bridge.close)
        self.client = Client(self.bridge)

    async def test_file_clipboard_and_download_share_real_manifests_without_a_model(self):
        source = self.root / "source.pdf"; content = b"%PDF-" + b"test-data" * 100000; source.write_bytes(content)
        image = io.BytesIO(); Image.new("RGB", (32, 24), "red").save(image, "PNG")
        ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        items = await attachments.upload(self.client, [{"path": str(source)}, {"data": "data:image/png;base64," + base64.b64encode(image.getvalue()).decode()}], ids)
        self.assertTrue(all(item["available"] for item in items)); self.assertTrue(items[1]["preview"].startswith("data:image/jpeg"))
        self.assertIsNone(self.bridge.engine); self.assertIsNone(self.bridge.protocol_task)
        self.assertNotIn(str(source), str(self.client.calls))
        blocks, saved = prepare_attachments(self.work, self.sid, ids)
        self.assertEqual(len(saved), 2); self.assertEqual(blocks[1]["type"], "image")
        source.write_bytes(b"changed locally")
        destination = self.root / "download.pdf"; destination.write_bytes(b"old")
        await attachments.download(self.client, ids[0], str(destination))
        self.assertEqual(destination.read_bytes(), content)
        self.assertFalse(list(self.root.glob('.xuloom-download-*')))

    async def test_unfinished_wrong_owner_bad_offset_and_restart_are_not_ready(self):
        identity = str(uuid.uuid4())
        await self.bridge.dispatch("attachments.begin", {"id": identity, "name": "a.txt", "size": 3})
        for method, params in [("attachments.write", {"id": str(uuid.uuid4()), "offset": 0, "data": "YWJj"}), ("attachments.begin", {"id": str(uuid.uuid4()), "name": "b", "size": 0}), ("attachments.write", {"id": identity, "offset": 1, "data": "YWJj"})]:
            with self.assertRaises(ValueError): await self.bridge.dispatch(method, params)
        self.assertIsNotNone(self.bridge.attachment_upload)
        with self.assertRaises(ValueError): prepare_attachments(self.work, self.sid, [identity])
        with self.assertRaises(ValueError): await self.bridge.dispatch('prompt', {'text':'must not start'})
        await self.bridge.dispatch('stop', {})
        self.assertIsNone(self.bridge.attachment_upload); self.assertIsNone(self.bridge.engine)
        restored = DesktopBridge(lambda e: None, self.work); restored.resume_id = self.sid; self.addAsyncCleanup(restored.close)
        item = (await restored.dispatch('attachments.list', {'ids':[identity]}))['items'][0]
        self.assertFalse(item['available']); self.assertEqual(item['state'], 'interrupted')
        self.assertFalse(list(attachment_directory(self.work, self.sid).glob('*.part')))

    async def test_controlled_history_and_existing_os_writer_are_rejected_before_upload(self):
        identity = str(uuid.uuid4()); params = {'id':identity,'name':'a.txt','size':0}
        with SessionManager(cwd=str(self.work)) as manager:
            manager.acquire_writer(self.sid)
            with self.assertRaises(SessionBusyError): await self.bridge.dispatch('attachments.begin', params)
        with SessionManager(cwd=str(self.work)) as manager:
            manager.load_session_state(self.sid); manager.save_session_state(self.sid, [], {'delegation':{'authority_required':True}})
        with self.assertRaises(ValueError): await self.bridge.dispatch('attachments.begin',params)
        self.assertFalse(attachment_directory(self.work,self.sid).exists()); self.assertIsNone(self.bridge.engine)

    async def test_lost_commit_ack_restores_ready_id_without_replaying_submission(self):
        source=self.root/'a.txt';source.write_bytes(b'recover me');identity=str(uuid.uuid4());original=self.client.request
        async def lost(method,params,**kwargs):
            value=await original(method,params,**kwargs)
            if method=='attachments.commit':raise RemoteDisconnected('lost acknowledgement')
            return value
        self.client.request=lost
        with self.assertRaises(RemoteDisconnected):await attachments.upload(self.client,[{'path':str(source)}],[identity])
        restored=DesktopBridge(lambda e:None,self.work);restored.resume_id=self.sid;self.addAsyncCleanup(restored.close)
        item=(await restored.dispatch('attachments.list',{'ids':[identity]}))['items'][0]
        self.assertTrue(item['available']);self.assertEqual(item['sha256'],hashlib.sha256(b'recover me').hexdigest())
        with SessionManager(cwd=str(self.work)) as manager:self.assertEqual(manager.load_session_state(self.sid)['messages'],[])
        self.assertEqual(sum(method=='attachments.commit' for method,_ in self.client.calls),1)

    async def test_digest_and_disk_failure_never_publish_incomplete_input(self):
        identity=str(uuid.uuid4());await self.bridge.dispatch('attachments.begin',{'id':identity,'name':'a.txt','size':3})
        await self.bridge.dispatch('attachments.write',{'id':identity,'offset':0,'data':'YWJj'})
        with self.assertRaises(ValueError):await self.bridge.dispatch('attachments.commit',{'id':identity,'sha256':'0'*64})
        with patch('xuloom.desktop.attachment_transfer.atomic_json',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):await self.bridge.dispatch('attachments.commit',{'id':identity,'sha256':hashlib.sha256(b'abc').hexdigest()})
        with self.assertRaises(ValueError):prepare_attachments(self.work,self.sid,[identity])
        await self.bridge.dispatch('stop',{})
        self.assertFalse((await self.bridge.dispatch('attachments.list',{'ids':[identity]}))['items'][0]['available'])

    async def test_interrupted_download_preserves_destination_and_removes_partial_file(self):
        source=self.root/'a.bin';source.write_bytes(b'a'*700000);identity=str(uuid.uuid4())
        await attachments.upload(self.client,[{'path':str(source)}],[identity])
        original=self.client.request
        async def broken(method,params,**kwargs):
            if method=='attachments.read' and params['offset']>0:raise RemoteDisconnected('disconnected')
            return await original(method,params,**kwargs)
        self.client.request=broken;target=self.root/'target.bin';target.write_bytes(b'keep me')
        with self.assertRaises(RemoteDisconnected):await attachments.download(self.client,identity,str(target))
        self.assertEqual(target.read_bytes(),b'keep me');self.assertFalse(list(self.root.glob('.xuloom-download-*')))

    async def test_cancel_before_begin_and_during_download_never_creates_a_late_effect(self):
        source = self.root / 'a.bin'; source.write_bytes(b'a' * 700000)
        cancelled = asyncio.Event(); cancelled.set()
        with self.assertRaisesRegex(ValueError, 'cancelled'):
            await attachments.upload(self.client, [{'path': str(source)}], cancelled=cancelled)
        self.assertFalse(any(method == 'attachments.begin' for method, _ in self.client.calls))
        cancelled.clear(); identity = str(uuid.uuid4())
        await attachments.upload(self.client, [{'path': str(source)}], [identity])
        original = self.client.request
        async def cancelling(method, params, **kwargs):
            result = await original(method, params, **kwargs)
            if method == 'attachments.read': cancelled.set()
            return result
        self.client.request = cancelling
        target = self.root / 'keep.bin'; target.write_bytes(b'original')
        with self.assertRaisesRegex(ValueError, 'cancelled'):
            await attachments.download(self.client, identity, str(target), cancelled=cancelled)
        self.assertEqual(target.read_bytes(), b'original')
        self.assertFalse(list(self.root.glob('.xuloom-download-*')))

    async def test_cancelled_commit_settles_disk_thread_before_releasing_writer(self):
        source = self.root / 'a.txt'; source.write_bytes(b'complete before cancellation')
        from xuloom.desktop import attachment_transfer
        original = attachment_transfer.commit
        entered, release = threading.Event(), threading.Event()
        def delayed(upload, digest):
            entered.set(); release.wait(5); return original(upload, digest)
        with patch.object(attachment_transfer, 'commit', delayed):
            identity = str(uuid.uuid4())
            task = asyncio.create_task(attachments.upload(self.client, [{'path': str(source)}], [identity]))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            task.cancel(); await asyncio.sleep(.03)
            self.assertFalse(task.done(), 'Disk publication must settle before cleanup')
            release.set()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertIsNone(self.bridge.attachment_upload)
        item = (await self.bridge.dispatch('attachments.list', {'ids': [identity]}))['items'][0]
        self.assertTrue(item['available'])
        with SessionManager(cwd=str(self.work)) as manager: manager.acquire_writer(self.sid)
        self.assertFalse(list(attachment_directory(self.work, self.sid).glob('*.part')))

    async def test_source_modified_during_upload_is_not_published_and_bad_image_is_not_input(self):
        source = self.root / 'a.txt'; source.write_bytes(b'first')
        original = self.client.request
        async def mutate(method, params, **kwargs):
            result = await original(method, params, **kwargs)
            if method == 'attachments.write': source.write_bytes(b'other')
            return result
        self.client.request = mutate; identity = str(uuid.uuid4())
        with self.assertRaisesRegex(ValueError, 'changed'):
            await attachments.upload(self.client, [{'path': str(source)}], [identity])
        self.assertFalse((await self.bridge.dispatch('attachments.list', {'ids': [identity]}))['items'][0]['available'])
        self.client.request = original
        source = self.root / 'broken.png'; source.write_bytes(b'not an image'); identity = str(uuid.uuid4())
        with self.assertRaises(OSError): await attachments.upload(self.client, [{'path': str(source)}], [identity])
        with self.assertRaises(ValueError): prepare_attachments(self.work, self.sid, [identity])


if __name__ == '__main__':unittest.main()
