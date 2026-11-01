"""System OpenSSH, actual remote bridge/Chromium and owned model/image receipts."""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import threading
from pathlib import Path
import unittest

if __package__:
    from . import test_remote_ssh as wire
    from .remote_capture_api import CaptureModel
else:
    import test_remote_ssh as wire
    from remote_capture_api import CaptureModel


@unittest.skipUnless(os.environ.get('XULOOM_TEST_BROWSER_RUNTIME')=='1' and wire.ssh_fixture.paramiko and shutil.which('ssh'),
    'optional Chromium, dev-only Paramiko and OpenSSH are required')
class RemoteCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.wire=wire.SSHWireTests();await self.wire.asyncSetUp()
        # The model fixture is shared with App/CLI checks; product execution is real.
        self.model=CaptureModel(self.wire.local.base)
        self.wire.local.response_handler=self.model
        self.browser_requested=threading.Event()
        def web(handler):
            self.browser_requested.set()
            handler.send_response(200);handler.send_header('Content-Type','text/html');handler.end_headers()
            handler.wfile.write(b'<html><body style="margin:0;background:#aabbcc">Owned remote web</body></html>')
            return True
        self.wire.local.web_handler=web
        (self.wire.remote/'page.html').write_text('<html><title>REMOTE_RENDER</title><link rel="stylesheet" href="style.css"><body>Remote page</body></html>',encoding='utf-8')
        (self.wire.remote/'style.css').write_text('body{margin:0;background:#aabbcc}',encoding='utf-8')

    async def asyncTearDown(self):
        try: await self.wire.asyncTearDown()
        except OSError as error:
            # Windows IME helpers can outlive a closed headless browser and keep
            # its inherited cwd open. Never kill a user's input-method process.
            if os.name != 'nt' or getattr(error,'winerror',None) != 32: raise
            root=self.wire.root.resolve()
            assert root.parent==Path(tempfile.gettempdir()).resolve() and root.name.startswith('xuloom connections ')
            started=time.monotonic()
            while time.monotonic()-started<120:
                await asyncio.sleep(.25)
                try:
                    self.wire.local.temp.cleanup()
                    print('Windows inherited directory handle released after %.1fs; no process was terminated' % (time.monotonic()-started),file=sys.stderr)
                    return
                except OSError as pending:
                    if getattr(pending,'winerror',None) != 32: raise
            raise

    async def run_remote(self, mode, permission='full'):
        events=[];queue=asyncio.Queue()
        def receive(event):events.append(event);queue.put_nowait(event)
        client=await self.wire.connect(callback=receive)
        identity=(await client.request('sessions.create'))['sessionId']
        await client.request('initialize',{'sessionId':identity,'permissionMode':permission})
        await client.request('prompt',{'text':'REMOTE_CAPTURE_'+mode})
        async def finished():
            while True:
                event=await queue.get()
                if event['type']=='turn_finished':return event
        await asyncio.wait_for(finished(),40)
        result=next(row['result'] for row in reversed(events) if row['type']=='final')
        self.assertTrue(result['ok'],result)
        self.assertFalse(any(row['type']=='browser_capture' for row in events),'A remote turn attempted local Electron capture')
        return client,identity,events

    async def test_remote_html_reaches_model_as_pixels_and_can_be_reopened(self):
        client,identity,events=await self.run_remote('FILE')
        self.assertEqual(self.model.captures[-1]['images'],1)
        data=self.wire.root/'remote-data'
        screenshots=list(data.rglob('screenshot-*.png'))
        self.assertEqual(len(screenshots),1);self.assertFalse(list((self.wire.root/'local-data').rglob('screenshot-*.png')))
        await client.aclose()
        restored=await self.wire.connect()
        history=await restored.request('initialize',{'sessionId':identity})
        self.assertIn('REMOTE_CAPTURE_FILE_VERIFIED',str(history))
        self.assertIn('playwright-hidden',str(history))

    async def test_missing_remote_html_is_a_failed_tool_without_a_fake_image(self):
        _,_,events=await self.run_remote('MISSING')
        self.assertEqual(self.model.captures[-1]['images'],0)
        self.assertFalse(list((self.wire.root/'remote-data').rglob('screenshot-*.png')))
        self.assertIn('failed',json.dumps(events).lower())

    async def test_text_only_remote_model_receives_a_path_without_fake_vision(self):
        configuration=self.wire.root/'remote-config/config.json'
        value=json.loads(configuration.read_text(encoding='utf-8'))
        value['model_connections'][0]['model_inputs']={'opaque-model':['text']}
        configuration.write_text(json.dumps(value),encoding='utf-8')
        await self.run_remote('TEXT')
        self.assertEqual(self.model.captures[-1]['images'],0)
        self.assertIn('Visual inspection unavailable',str(self.model.captures[-1]['tool']))
        self.assertEqual(len(list((self.wire.root/'remote-data').rglob('screenshot-*.png'))),1)

    async def test_cli_uses_remote_chromium_and_the_same_native_image_pipeline(self):
        code,events,error=await self.wire.run_cli('exec','test','REMOTE_CAPTURE_FILE','--permission','full')
        self.assertEqual(code,0,error+str(events))
        self.assertIn('REMOTE_CAPTURE_FILE_VERIFIED',str(events))
        self.assertEqual(self.model.captures[-1]['images'],1)
        self.assertFalse(list((self.wire.root/'local-data').rglob('screenshot-*.png')))

    async def test_remote_permission_denial_never_launches_a_capture(self):
        queue=asyncio.Queue();events=[]
        def receive(event):events.append(event);queue.put_nowait(event)
        client=await self.wire.connect(callback=receive)
        identity=(await client.request('sessions.create'))['sessionId']
        await client.request('initialize',{'sessionId':identity,'permissionMode':'ask'})
        await client.request('prompt',{'text':'REMOTE_CAPTURE_DENY'})
        async def finished():
            while True:
                event=await queue.get()
                if event['type']=='permission':
                    self.assertEqual(event['request']['tool_name'],'browser_tool')
                    await client.request('respond',{'requestId':event['requestId'],'result':'deny'})
                if event['type']=='turn_finished':return
        await asyncio.wait_for(finished(),30)
        self.assertEqual(self.model.captures[-1]['images'],0)
        self.assertIn('permission_behavior: deny',self.model.captures[-1]['tool']['content'])
        self.assertFalse(list((self.wire.root/'remote-data').rglob('screenshot-*.png')))

    async def test_remote_stop_cancels_a_loaded_page_without_publishing_pixels(self):
        events=[];queue=asyncio.Queue()
        def receive(event):events.append(event);queue.put_nowait(event)
        client=await self.wire.connect(callback=receive)
        identity=(await client.request('sessions.create'))['sessionId']
        await client.request('initialize',{'sessionId':identity,'permissionMode':'full'})
        await client.request('prompt',{'text':'REMOTE_CAPTURE_WAIT'})
        self.assertTrue(await asyncio.to_thread(self.browser_requested.wait,20))
        await client.request('stop')
        self.assertTrue(any(row['type']=='turn_cancelled' for row in events),events)
        self.assertFalse(self.model.captures)
        self.assertFalse(list((self.wire.root/'remote-data').rglob('screenshot-*.png')))
        await client.aclose()
        observer=await self.wire.connect()
        status=await observer.request('session.status',{'sessionId':identity})
        self.assertEqual(status['writer'],'available')
