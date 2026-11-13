import os
import asyncio
import tempfile
import unittest
from unittest.mock import patch

from xuloom.core.network import model_http_client, create_model_client


class ModelNetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_sdk_clients_observe_connection_limit_changes_without_recreation(self):
        import importlib
        settings={'model_request_limits':{'concurrency':2}}
        entered, release = asyncio.Event(), asyncio.Event()
        requests=[]
        async def response(request):
            requests.append(request)
            if len(requests)==1:
                entered.set();await release.wait()
            return wire_http.Response(200,json={'fixture':True})
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,{'XULOOM_DATA_DIR':folder}), patch('xuloom.core.network.load_config',side_effect=lambda **kwargs:settings):
            clients=[create_model_client(provider='openai',model='fixture',api_base_url='http://127.0.0.1/v1',api_key='fixture',network_mode='direct',request_owner=name) for name in ('a','b')]
            wire_http=importlib.import_module(next(cls.__module__.split('.')[0] for cls in clients[0]._client.__class__.__mro__ if cls.__module__.split('.')[0] in {'httpx','httpx2'}))
            for client in clients: client._client._transport=wire_http.MockTransport(response)
            calls=[]
            try:
                calls.append(asyncio.create_task(clients[0]._client.get('http://127.0.0.1/v1/fixture')))
                await asyncio.wait_for(entered.wait(),2)
                settings['model_request_limits']['concurrency']=1
                calls.append(asyncio.create_task(clients[1]._client.get('http://127.0.0.1/v1/fixture')))
                await asyncio.sleep(.12)
                self.assertEqual(len(requests),1)
                release.set()
                await asyncio.wait_for(asyncio.gather(*calls),3)
                self.assertEqual(len(requests),2)
            finally:
                release.set()
                for task in calls:
                    if not task.done(): task.cancel()
                await asyncio.gather(*calls,return_exceptions=True)
                for client in clients: await client.close()

    async def test_sdk_factories_use_their_compatible_http_library(self):
        from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient as AnthropicHttp
        from openai import AsyncOpenAI, DefaultAsyncHttpxClient as OpenAIHttp
        for sdk, factory in [(AsyncAnthropic, AnthropicHttp), (AsyncOpenAI, OpenAIHttp)]:
            async with sdk(api_key="fixture", http_client=model_http_client(factory, network_mode='direct')) as client:
                self.assertFalse(client._client.trust_env)

    async def test_direct_mode_ignores_implicit_windows_proxy(self):
        with patch.dict(os.environ, {"XULOOM_NETWORK_MODE": ""}), patch("xuloom.core.network.load_config", return_value={}), patch("httpx._utils.getproxies", return_value={"https": "http://127.0.0.1:1"}) as proxies:
            async with model_http_client() as client:
                self.assertFalse(client.trust_env)
                proxies.assert_not_called()

    async def test_system_mode_explicitly_enables_proxy_discovery(self):
        with patch.dict(os.environ, {"XULOOM_NETWORK_MODE": ""}), patch("xuloom.core.network.load_config", return_value={"network_mode": "system"}), patch("httpx._utils.getproxies", return_value={"https": "http://127.0.0.1:1"}) as proxies:
            async with model_http_client() as client:
                self.assertTrue(client.trust_env)
                proxies.assert_called_once()

    async def test_automatic_route_remembers_success_and_recovers_both_directions(self):
        import httpx
        from xuloom.core.request_flow import connection_id

        attempts = []
        working = "system"

        async def send(transport, request):
            route = "system" if transport._pool.__class__.__name__ == "AsyncHTTPProxy" else "direct"
            attempts.append(route)
            if route != working:
                raise httpx.ConnectTimeout("fixture connect failure", request=request)
            return httpx.Response(200, json={"ok": True})

        identity = connection_id("https://model.example/v1", "fixture")
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XULOOM_DATA_DIR": directory}), \
             patch("httpx._utils.getproxies", return_value={"https": "http://127.0.0.1:7897"}), \
             patch.object(httpx.AsyncHTTPTransport, "handle_async_request", send):
            for expected in (["direct", "system"], ["system"], ["system", "direct"], ["direct"]):
                if expected == ["system", "direct"]:
                    working = "direct"
                attempts.clear()
                async with model_http_client(network_mode="auto", route_identity=identity) as client:
                    reply = await client.post("https://model.example/v1/chat/completions", json={"fixture": True})
                self.assertEqual(reply.status_code, 200)
                self.assertEqual(attempts, expected)

    async def test_sdk_routes_exhaust_once_and_preserve_http_and_stream_errors(self):
        import importlib
        from xuloom.core.network import model_error_message
        from xuloom.core.network_routes import RoutesUnavailable, exhausted_routes

        for provider, host, module in (("openai", "https://openai.invalid/v1", "openai"),
                                      ("anthropic", "https://anthropic.invalid", "anthropic")):
            sdk = importlib.import_module(module)
            attempts = []
            failure = "connect"
            with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XULOOM_DATA_DIR": directory}):
                client = create_model_client(provider=provider, model="fixture", api_base_url=host,
                    api_key="fixture", network_mode="auto")
                library = importlib.import_module(client._client._transport.direct.__class__.__module__.split('.')[0])

                class Interrupted(library.AsyncByteStream):
                    async def __aiter__(self):
                        yield b"data: first\n\n"
                        raise library.ReadError("response interrupted")

                async def send(transport, request):
                    attempts.append(transport)
                    if failure == "connect":
                        raise library.ConnectError("fixture-key-must-not-appear", request=request)
                    if failure == "read":
                        raise library.ReadTimeout("response wait elapsed", request=request)
                    if failure == "stream":
                        return library.Response(200, stream=Interrupted())
                    return library.Response(failure, json={"error": {"message": "denied"}})

                try:
                    with patch(library.__name__ + "._utils.getproxies", return_value={"https": "http://127.0.0.1:7897"}), \
                         patch.object(library.AsyncHTTPTransport, "handle_async_request", send):
                        async def request():
                            if provider == "openai":
                                return await client.chat.completions.create(model="fixture", messages=[{"role": "user", "content": "test"}])
                            return await client.messages.create(model="fixture", messages=[{"role": "user", "content": "test"}], max_tokens=16)

                        with self.assertRaises((RoutesUnavailable, sdk.APIConnectionError)) as raised:
                            await request()
                        self.assertEqual(len(attempts), 2)
                        self.assertIsNotNone(exhausted_routes(raised.exception))
                        message = model_error_message(raised.exception)
                        self.assertIn("直连", message)
                        self.assertIn("系统代理", message)
                        self.assertNotIn("fixture-key-must-not-appear", message)
                        for failure in (401, 429, 503):
                            attempts.clear()
                            with self.assertRaises(sdk.APIStatusError) as rejected:
                                await request()
                            self.assertEqual(rejected.exception.status_code, failure)
                            self.assertEqual(len(attempts), 1)
                        attempts.clear()
                        failure = "read"
                        with self.assertRaises(sdk.APITimeoutError):
                            await request()
                        self.assertEqual(len(attempts), 1)
                        attempts.clear()
                        failure = "stream"
                        with self.assertRaises(library.ReadError):
                            async with client._client.stream("POST", host) as response:
                                async for _ in response.aiter_bytes():
                                    pass
                        self.assertEqual(len(attempts), 1)
                finally:
                    await client.close()

    async def test_model_discovery_and_model_requests_share_persisted_route(self):
        import httpx
        from xuloom.core.model_catalog import discover_models
        from xuloom.core.request_flow import connection_id
        from xuloom.core.network_routes import preferred_route, route_file

        base = "https://model.example/v1"
        identity = connection_id(base, "fixture")
        cfg = {"model_connections": [{"id": "fixture", "provider": "openai", "base_url": base,
            "model": "fixture", "models": ["fixture"], "api_key": "fixture", "network_mode": "auto"}]}
        attempts = []

        async def send(transport, request):
            route = "system" if transport._pool.__class__.__name__ == "AsyncHTTPProxy" else "direct"
            attempts.append(route)
            if route == "direct":
                raise httpx.ConnectError("fixture", request=request)
            return httpx.Response(200, json={"data": [{"id": "fixture"}]})

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XULOOM_DATA_DIR": directory}), \
             patch("httpx._utils.getproxies", return_value={"https": "http://127.0.0.1:7897", "no": "bypass.example"}), \
             patch.object(httpx.AsyncHTTPTransport, "handle_async_request", send):
            self.assertEqual((await discover_models(cfg, {"connectionId": "fixture"}))["models"], ["fixture"])
            self.assertEqual(preferred_route(route_file(identity)), "system")
            self.assertEqual(preferred_route(route_file(connection_id(base, "different-key"))), "direct")
            async with model_http_client(network_mode="auto", route_identity=identity) as client:
                await client.post(base + "/chat/completions", json={"fixture": True})
            self.assertEqual(attempts, ["direct", "system", "system"])
            for host in ("bypass.example", "localhost", "127.0.0.1"):
                attempts.clear()
                async with model_http_client(network_mode="auto") as client:
                    with self.assertRaises(ConnectionError):
                        await client.get(f"https://{host}/v1/models")
                self.assertEqual(attempts, ["direct"])

    async def test_anthropic_engine_does_not_repeat_an_exhausted_request(self):
        from xuloom import EngineConfig, QueryEngine
        from xuloom.core.network_routes import RoutesUnavailable
        from unittest.mock import AsyncMock

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XULOOM_DATA_DIR": directory}):
            engine = QueryEngine(EngineConfig(cwd=directory, model_provider="anthropic", model="fixture",
                api_base_url="https://model.example", api_key="fixture", network_mode="auto",
                fallback_model="other", collaboration_mode="solo", bundled_plugins=False))
            exhausted = RoutesUnavailable([("direct", "ConnectTimeout"), ("system", "ProxyError")])
            with patch.object(engine.client.messages, "stream", side_effect=exhausted) as stream, \
                 patch.object(engine.client.messages, "create", new_callable=AsyncMock) as nonstream:
                try:
                    result = await engine.run("Say hello", sys_print_callback=lambda _: None)
                    self.assertEqual(result.reason, "api_error")
                    self.assertIn("系统代理（ProxyError）", result.text)
                    stream.assert_called_once()
                    nonstream.assert_not_awaited()
                finally:
                    await engine.aclose()
