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
