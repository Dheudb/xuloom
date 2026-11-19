"""Offline web routing regressions; never contact public sites or model providers."""
import asyncio
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from xuloom.tools.web_network import _RoutedClient, search_network
from xuloom.tools.web_tool import WebFetchTool, extract_page

PROXIES = {"https": "http://localhost:7897", "http": "http://localhost:7897"}


class WebRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.environment = patch.dict(os.environ, {"XULOOM_DATA_DIR": self.temp.name, "XULOOM_WEB_NETWORK_MODE": "auto"})
        self.environment.start(); self.addCleanup(self.environment.stop)

    def fetch(self):
        tool = WebFetchTool()
        tool.context = {"native_search_options": lambda: {"profile": {"backend": "builtin"}, "network_mode": "direct", "api_key": "NEVER_SEND_MODEL_KEY"}}
        return tool

    async def test_model_direct_does_not_disable_host_proxy_and_failed_body_retries_other_route(self):
        calls, clients = [], []
        class Broken(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"partial untrusted output"
                raise httpx.ReadTimeout("body stalled")
        def client(**kwargs):
            route = "system" if kwargs.get("proxy") else "direct"
            def handle(request):
                calls.append((route, str(request.url))); self.assertNotIn("authorization", request.headers)
                return httpx.Response(200, stream=Broken()) if route == "system" else httpx.Response(200, text="complete evidence")
            value = httpx.AsyncClient(transport=httpx.MockTransport(handle)); clients.append(value); return value
        with patch("xuloom.tools.web_network.getproxies", return_value=PROXIES), patch("xuloom.tools.web_network.web_client", side_effect=client):
            result = await self.fetch().execute("https://paper.invalid/a")
            self.assertTrue(result["ok"], result)
            self.assertNotIn("partial untrusted", result["content"])
            self.assertEqual([r for r,u in calls], ["system", "direct"])
            self.assertEqual(result["metadata"]["route_used"], "direct")
            calls.clear(); await self.fetch().execute("https://paper.invalid/b")
            self.assertEqual([r for r,u in calls], ["direct"], "Remember success by site, not model API or URL path")
            calls.clear(); await self.fetch().execute("https://other.invalid/a")
            self.assertEqual([r for r,u in calls], ["system", "direct"])
        self.assertTrue(all(c.is_closed for c in clients))
        cache = list(Path(self.temp.name).rglob('*.json')); self.assertTrue(cache)
        self.assertNotIn('paper.invalid', ''.join(p.read_text() for p in cache))
        self.assertNotIn('NEVER_SEND_MODEL_KEY', ''.join(p.read_text() for p in cache))

    async def test_redirect_target_status_and_no_network_fallback_for_site_denials(self):
        for status, code in [(403,'access_denied'),(404,'not_found'),(429,'rate_limited'),(451,'legal_restriction')]:
            calls = []
            def client(**kwargs):
                def handle(request):
                    calls.append(str(request.url))
                    return httpx.Response(302, headers={"Location":"https://final.invalid/profile"}) if request.url.host=='first.invalid' else httpx.Response(status,headers={"Retry-After":"30"})
                return httpx.AsyncClient(transport=httpx.MockTransport(handle))
            with patch("xuloom.tools.web_network.getproxies", return_value=PROXIES), patch("xuloom.tools.web_network.web_client", side_effect=client):
                result = await self.fetch().execute('https://first.invalid/profile')
            self.assertFalse(result['ok']);self.assertEqual(result['metadata']['code'],code)
            self.assertEqual(result['metadata']['final_url'],'https://final.invalid/profile')
            self.assertEqual(len(calls),2,'Do not switch proxies to retry a site refusal')
            if status==451:self.assertIn('existence', result['content'])

    async def test_no_proxy_and_explicit_web_direct_are_respected(self):
        for proxies, mode in [(dict(PROXIES,no='*'),'auto'),(PROXIES,'direct')]:
            routes=[]
            def client(**kwargs):
                routes.append(bool(kwargs.get('proxy')))
                return httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,text='ok')))
            with patch.dict(os.environ,{'XULOOM_WEB_NETWORK_MODE':mode}),patch('xuloom.tools.web_network.getproxies',return_value=proxies),patch('xuloom.tools.web_network.web_client',side_effect=client):
                self.assertTrue((await self.fetch().execute('https://public.invalid'))['ok'])
            self.assertEqual(routes,[False])

    async def test_cancellation_closes_stream_without_starting_another_route(self):
        entered=asyncio.Event();clients=[]
        class Slow(httpx.AsyncByteStream):
            async def __aiter__(self):
                entered.set();await asyncio.Event().wait();yield b'unreachable'
        def client(**kwargs):
            value=httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,stream=Slow())));clients.append(value);return value
        with patch('xuloom.tools.web_network.getproxies',return_value=PROXIES),patch('xuloom.tools.web_network.web_client',side_effect=client):
            task=asyncio.create_task(self.fetch().execute('https://public.invalid'))
            await entered.wait();task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(len(clients),1);self.assertTrue(clients[0].is_closed)

    def test_ddgs_prefers_proxy_then_recovers_and_relearns_each_site(self):
        _,proxies,info=search_network('auto');proxies=PROXIES
        calls=[];bad={'system'}
        class Client:
            proxy=None
            def request(self,method,url,*args,**kwargs):
                route='system' if self.proxy else 'direct';calls.append((route,url))
                if route in bad:raise ConnectionResetError('network reset')
                return SimpleNamespace(status_code=200,headers={},text='evidence')
        routed=_RoutedClient(Client(),proxies,info)
        routed.request('POST','https://search.invalid/query')
        self.assertEqual([r for r,u in calls],['system','direct'])
        calls.clear();routed.request('POST','https://search.invalid/next');self.assertEqual([r for r,u in calls],['direct'])
        bad.clear();bad.add('direct');calls.clear();routed.request('POST','https://search.invalid/later')
        self.assertEqual([r for r,u in calls],['direct','system'])
        calls.clear();routed.request('GET','https://other.invalid');self.assertEqual([r for r,u in calls],['system'])

    async def test_actual_loopback_http_proxy_receives_no_model_key(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append((self.path, self.headers.get('Authorization')))
                self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(b'<p>Verified proxy evidence</p>')
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        proxy=f'http://127.0.0.1:{server.server_port}'
        with patch('xuloom.tools.web_network.getproxies',return_value={'http':proxy}):
            result=await self.fetch().execute('http://offline-fixture.invalid/page')
        self.assertTrue(result['ok'],result);self.assertIn('Verified proxy evidence',result['content'])
        self.assertEqual(received,[('http://offline-fixture.invalid/page',None)])
        self.assertEqual(result['metadata']['route_used'],'system')

    async def test_entire_fetch_deadline_closes_in_flight_stream(self):
        clients=[]
        class Slow(httpx.AsyncByteStream):
            async def __aiter__(self):
                await asyncio.Event().wait();yield b'unreachable'
        def client(**kwargs):
            value=httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(200,stream=Slow())));clients.append(value);return value
        with patch('xuloom.tools.web_tool.FETCH_TOTAL_TIMEOUT',.05),patch('xuloom.tools.web_network.getproxies',return_value=PROXIES),patch('xuloom.tools.web_network.web_client',side_effect=client):
            result=await self.fetch().execute('https://public.invalid')
        self.assertFalse(result['ok']);self.assertEqual(result['metadata']['code'],'timeout')
        self.assertEqual(len(clients),1);self.assertTrue(clients[0].is_closed)

    async def test_all_failed_routes_are_retained_without_secrets_or_success_cache(self):
        proxy='http://user:opaque-proxy-password@localhost:7897'
        calls=[]
        def client(**kwargs):
            def handle(request):
                calls.append(bool(kwargs.get('proxy')))
                raise httpx.ReadError('connection reset through opaque-proxy-password')
            return httpx.AsyncClient(transport=httpx.MockTransport(handle))
        with patch('xuloom.tools.web_network.getproxies',return_value={'https':proxy}),patch('xuloom.tools.web_network.web_client',side_effect=client):
            result=await self.fetch().execute('https://broken.invalid')
        self.assertFalse(result['ok']);self.assertEqual(calls,[True,False])
        self.assertEqual([r['route'] for r in result['metadata']['route_failures']],['system','direct'])
        self.assertNotIn('opaque-proxy-password',str(result))
        self.assertEqual(list(Path(self.temp.name).rglob('*.json')),[])

    def test_corrupt_route_cache_does_not_break_default_proxy_choice(self):
        from xuloom.tools.web_network import WebRoutes
        info={'network_mode':'auto'};policy=WebRoutes(PROXIES,info)
        path=policy.path('https://public.invalid');path.parent.mkdir(parents=True);path.write_text('{"route": []}')
        self.assertEqual(policy.routes('https://public.invalid'),['system','direct'])

    def test_static_extraction_keeps_real_relative_and_absolute_links(self):
        page='<html><title>Paper</title><nav><a href="/noise">nav</a></nav><p>Source <a href="../papers/a?q=1">Real paper</a><a href="https://other.invalid/b">Another</a><a href="javascript:bad()">unsafe</a></p></html>'
        text,title=extract_page(page,True,'https://example.invalid/research/index')
        self.assertEqual(title,'Paper');self.assertIn('https://example.invalid/papers/a?q=1',text)
        self.assertIn('https://other.invalid/b',text);self.assertNotIn('javascript:',text);self.assertNotIn('/noise',text)


if __name__ == '__main__':unittest.main()
