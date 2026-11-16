"""Scaling service lifecycle without model calls."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from xuloom.scaling import daemon


class Service:
    def __init__(self, path):
        self.path, self.published_early, self.closed, self.idle = path, None, False, True

    async def start(self):
        await asyncio.sleep(.2)
        self.published_early = self.path.exists()

    async def dispatch(self, method, params):
        return {"method": method}

    def can_stop(self):
        return self.idle

    async def close(self):
        self.closed = True


class DaemonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="xuloom-scaling-daemon-")
        self.environment = patch.dict(os.environ, {"XULOOM_DATA_DIR": str(Path(self.tmp.name) / "data"), "XULOOM_SCALING_IDLE_SECONDS": "0.3"})
        self.environment.start()

    async def asyncTearDown(self):
        self.environment.stop(); self.tmp.cleanup()

    async def test_endpoint_is_published_after_start_and_idle_service_exits(self):
        path = daemon.endpoint("node")
        service = Service(path)
        await asyncio.wait_for(daemon.serve("node", service), 10)
        self.assertFalse(service.published_early)
        self.assertTrue(service.closed)
        self.assertFalse(path.exists())

    async def test_service_with_unfinished_work_stays_up(self):
        path = daemon.endpoint("control")
        service = Service(path); service.idle = False
        task = asyncio.create_task(daemon.serve("control", service))
        for _ in range(100):
            if path.exists(): break
            await asyncio.sleep(.05)
        address = __import__("json").loads(path.read_text(encoding="utf-8"))
        self.assertEqual(await daemon.call(address, "status"), {"method": "status"})
        await asyncio.sleep(1)
        self.assertFalse(task.done())
        service.idle = True
        await asyncio.wait_for(task, 10)
        self.assertFalse(path.exists())

    async def test_idle_shutdown_does_not_cut_a_request_in_flight(self):
        path = daemon.endpoint("control")
        service = Service(path)
        entered, release = asyncio.Event(), asyncio.Event()

        async def dispatch(method, params):
            entered.set()
            await release.wait()
            return {"saved": True}

        service.dispatch = dispatch
        server = asyncio.create_task(daemon.serve("control", service))
        request = None
        try:
            for _ in range(100):
                if path.exists():
                    break
                await asyncio.sleep(.05)
            request = asyncio.create_task(daemon.call(json.loads(path.read_text()), "create"))
            await asyncio.wait_for(entered.wait(), 3)
            await asyncio.sleep(.75)
            self.assertFalse(server.done())
            self.assertEqual(await daemon.call(json.loads(path.read_text()), "ping"), {"protocol": 1})
            release.set()
            self.assertEqual(await asyncio.wait_for(request, 3), {"saved": True})
            await asyncio.wait_for(server, 3)
        finally:
            release.set()
            server.cancel()
            await asyncio.gather(server, *([request] if request else []), return_exceptions=True)

    async def test_gateway_reconnects_a_refused_socket_without_replaying_uncertain_requests(self):
        gateway = daemon.Gateway(None)
        gateway.address = {"port": 1}
        replacement = {"port": 2}
        with patch.object(daemon, "connect", AsyncMock(return_value=replacement)) as connect, patch.object(
            daemon, "call", AsyncMock(side_effect=[ConnectionRefusedError(), {"started": True}, ConnectionError("reply lost")])
        ) as call:
            self.assertEqual(await gateway.dispatch("scaling.node.submit", {"id": "work"}), {"started": True})
            self.assertEqual(connect.await_count, 1)
            self.assertEqual([args.args[0] for args in call.await_args_list], [{"port": 1}, replacement])
            with self.assertRaisesRegex(ConnectionError, "reply lost"):
                await gateway.dispatch("scaling.node.submit", {"id": "uncertain"})
            self.assertEqual(call.await_count, 3)
            self.assertIsNone(gateway.address)
