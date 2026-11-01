"""Actual optional Chromium: isolated sessions, workspace assets and cancellation."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
import threading
import unittest

from PIL import Image
from xuloom.features.browser import BrowserSessionManager, PLAYWRIGHT_AVAILABLE


@unittest.skipUnless(PLAYWRIGHT_AVAILABLE and os.environ.get('XULOOM_TEST_BROWSER_RUNTIME') == '1',
    'set XULOOM_TEST_BROWSER_RUNTIME=1 with the optional browser extra and Chromium')
class ChromiumRenderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='xuloom-browser-render-')
        self.root = Path(self.temp.name); self.workspace = self.root / 'workspace'; self.workspace.mkdir()
        self.manager = BrowserSessionManager(str(self.root / 'captures'))
        self.requests = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                owner.requests.append({'path':self.path,'cookie':self.headers.get('Cookie')})
                self.send_response(404 if self.path == '/missing' else 200)
                self.send_header('Content-Type','text/html')
                self.send_header('Set-Cookie','owned-page=present; Path=/')
                self.end_headers(); self.wfile.write(b'<html><title>OWNED_WEB</title><body style="margin:0;background:#aabbcc">Owned web</body></html>')
        self.server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url='http://127.0.0.1:'+str(self.server.server_port)

    async def asyncTearDown(self):
        await self.manager.close()
        await asyncio.to_thread(self.server.shutdown);self.server.server_close();self.thread.join(3)
        self.temp.cleanup()

    async def render(self, **values):
        options=dict(cwd=str(self.workspace),width=800,height=600,wait_ms=0,full_page=False)
        options.update(values)
        return await self.manager.render(**options)

    def pixels(self, result, color=(170,187,204), size=(800,600)):
        with Image.open(result['path']) as image:
            self.assertEqual(image.size,size)
            self.assertEqual(image.convert('RGB').getpixel((790,590)),color)

    async def test_real_css_svg_and_console_are_rendered_without_node_or_file_origin(self):
        (self.workspace/'style.css').write_text('body{margin:0;background:#aabbcc}',encoding='utf-8')
        (self.workspace/'page.html').write_text('''<html><head><link rel="stylesheet" href="style.css"><title>OWNED_LOCAL</title></head><body>
<svg width="100" height="100"><circle cx="50" cy="50" r="40" fill="gold"/></svg>
<script>document.title+=':'+location.protocol+':'+typeof require+':'+typeof process;console.error('OWNED_CONSOLE');</script></body></html>''',encoding='utf-8')
        result=await self.render(file_path=str(self.workspace/'page.html'))
        self.pixels(result);self.assertEqual(result['backend'],'playwright-hidden')
        self.assertEqual(result['title'],'OWNED_LOCAL:https::undefined:undefined')
        self.assertIn('OWNED_CONSOLE',result['console_errors'])
        self.assertEqual(self.manager._render_contexts,set());self.assertEqual(self.manager._browser.contexts,[])

    async def test_workspace_file_protocol_and_hidden_assets_are_unavailable(self):
        outside=self.root/'outside.png'; Image.new('RGB',(800,600),'red').save(outside)
        secret=self.workspace/'.secret.txt';secret.write_text('OUTSIDE_SECRET',encoding='utf-8')
        (self.workspace/'page.html').write_text('''<html><title>START</title><body style="margin:0;background:#aabbcc">
<img style="width:800px;height:600px" src="'''+outside.as_uri()+'''" onerror="this.remove()">
<script>fetch('/.secret.txt').then(r=>document.title='HIDDEN_'+r.status)</script></body></html>''',encoding='utf-8')
        result=await self.render(file_path=str(self.workspace/'page.html'),wait_ms=100)
        self.pixels(result);self.assertEqual(result['title'],'HIDDEN_404')
        with self.assertRaises(ValueError):await self.render(file_path=str(self.root/'outside.html'))
        self.assertEqual(self.manager._browser.contexts,[])

    async def test_url_capture_does_not_inherit_or_retain_interactive_cookies(self):
        page=await self.manager.ensure_page()
        await page.context.add_cookies([{'url':self.url,'name':'interactive-secret','value':'do-not-share'}])
        for _ in range(2):self.pixels(await self.render(url=self.url+'/page'))
        self.assertTrue(self.requests);self.assertTrue(all(row['cookie'] is None for row in self.requests))
        self.assertEqual(self.manager._browser.contexts,[page.context])

    async def test_http_error_does_not_publish_an_image(self):
        with self.assertRaisesRegex(RuntimeError,'404'):await self.render(url=self.url+'/missing')
        self.assertEqual(list((self.root/'captures').glob('*.png')),[])
        self.assertEqual(self.manager._browser.contexts,[])

    async def test_cancellation_closes_the_owned_render_context(self):
        task=asyncio.create_task(self.manager.render(cwd=str(self.workspace),url=self.url+'/page',width=800,height=600,wait_ms=10000))
        async def entered():
            while not self.requests:await asyncio.sleep(.01)
        await asyncio.wait_for(entered(),15);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(self.manager._render_contexts,set());self.assertEqual(self.manager._browser.contexts,[])
        self.assertEqual(list((self.root/'captures').glob('*.png')),[])

    async def test_concurrent_captures_share_one_driver_but_no_context(self):
        first,second=await asyncio.gather(self.render(url=self.url+'/one'),self.render(url=self.url+'/two'))
        self.pixels(first);self.pixels(second);self.assertNotEqual(first['path'],second['path'])
        self.assertEqual(self.manager._browser.contexts,[]);self.assertEqual(self.manager._render_contexts,set())

    async def test_full_page_has_real_bottom_pixels_and_oversized_pages_are_explicit(self):
        entry=self.workspace/'long.html'
        entry.write_text('<html><body style="margin:0;background:#aabbcc;height:1800px"><div style="position:absolute;top:1700px;width:100%;height:100px;background:#123456">bottom</div></body></html>',encoding='utf-8')
        result=await self.render(file_path=str(entry),full_page=True)
        self.assertFalse(result['truncated'])
        with Image.open(result['path']) as image:
            self.assertEqual(image.size,(800,1800));self.assertEqual(image.convert('RGB').getpixel((400,1750)),(18,52,86))
        entry.write_text('<html><body style="margin:0;background:#aabbcc;height:10000px">very tall</body></html>',encoding='utf-8')
        bounded=await self.render(file_path=str(entry),full_page=True)
        self.assertTrue(bounded['truncated']);self.pixels(bounded)


if __name__=='__main__':unittest.main()
