import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import httpx

from xuloom.core.request_flow import RequestFlow, connection_id, http_client_type, limits, retry_delay


class RequestFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    async def asyncTearDown(self):
        self.temp.cleanup()

    def flow(self, key="fixture", **policy):
        return RequestFlow(connection_id("http://127.0.0.1/v1", key), {"concurrency": 1, **policy}, self.root)

    async def test_shared_capacity_fairness_cancellation_and_no_credentials_on_disk(self):
        first = await self.flow().acquire("session-a")
        a = asyncio.create_task(self.flow().acquire("session-a"))
        await asyncio.sleep(.08)
        b = asyncio.create_task(self.flow().acquire("session-b"))
        await asyncio.sleep(.08)
        cancelled = asyncio.create_task(self.flow().acquire("cancelled"))
        await asyncio.sleep(.08)
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
        self.assertFalse(a.done()); self.assertFalse(b.done())
        await first.release()
        second = await asyncio.wait_for(b, 3)
        self.assertFalse(a.done(), "another session gets a turn before the first floods the queue")
        await second.release()
        await (await asyncio.wait_for(a, 3)).release()
        raw = (self.flow().root / "state.json").read_text()
        self.assertNotIn("fixture", raw)
        self.assertNotIn("session-a", raw)
        self.assertEqual(json.loads(raw)["active"], [])
        self.assertEqual(json.loads(raw)["queue"], [])

    async def test_other_connections_do_not_block_and_rpm_is_shared(self):
        first = await self.flow().acquire("a")
        other = await asyncio.wait_for(self.flow("other").acquire("b"), 1)
        await other.release(); await first.release()
        flow = self.flow("rate", requests_per_minute=300)
        first = await flow.acquire("a"); start = time.monotonic(); await first.release()
        second = await flow.acquire("b")
        self.assertGreaterEqual(time.monotonic() - start, .17)
        await second.release()

    async def test_cooldown_is_connection_wide(self):
        await self.flow().cooldown(.2)
        start = time.monotonic()
        permit = await self.flow().acquire("a")
        self.assertGreaterEqual(time.monotonic() - start, .17)
        await permit.release()
        self.assertEqual(retry_delay({"retry-after-ms": "200"}), .2)
        self.assertEqual(retry_delay({"retry-after": "Thu, 01 Jan 1970 00:00:10 GMT"}, now=5), 5)
        self.assertEqual(retry_delay({"retry-after": "bad"}), 1)

    async def test_stricter_connection_limit_cannot_be_bypassed_by_an_older_client(self):
        high, low = self.flow(concurrency=2), self.flow(concurrency=1)
        first = await low.acquire('session-b')
        overtaking = asyncio.create_task(high.acquire('session-a'))
        try:
            await asyncio.sleep(.12)
            self.assertFalse(overtaking.done(), 'the older client must respect the active connection limit')
            await first.release()
            await (await asyncio.wait_for(overtaking, 2)).release()
        finally:
            await first.release()
            for task in (overtaking,):
                if task.done() and not task.cancelled() and task.exception() is None:
                    await task.result().release()
                else:
                    task.cancel()
            await asyncio.gather(overtaking, return_exceptions=True)

    async def test_lower_rpm_applies_to_the_previous_start_not_the_old_interval(self):
        first = await self.flow('lower-rate', requests_per_minute=600).acquire('a')
        start = time.monotonic()
        await first.release()
        permit = await self.flow('lower-rate', requests_per_minute=120).acquire('b')
        try:
            self.assertGreaterEqual(time.monotonic() - start, .4)
        finally:
            await permit.release()

    async def test_live_policy_changes_apply_to_an_already_waiting_request(self):
        settings = {'concurrency': 2}
        flow = RequestFlow(connection_id('http://127.0.0.1/v1','live'),lambda:settings,self.root)
        first = await flow.acquire('a')
        settings['concurrency'] = 1
        waiting = asyncio.create_task(flow.acquire('b'))
        try:
            await asyncio.sleep(.1)
            self.assertFalse(waiting.done())
            settings['concurrency'] = 2
            second = await asyncio.wait_for(waiting, 2)
            await second.release()
        finally:
            if not waiting.done(): waiting.cancel()
            await asyncio.gather(waiting,return_exceptions=True)
            await first.release()

    async def test_bad_nested_state_is_preserved_before_registration(self):
        base = dict(queue=[],active=[],turns=[],cooldown_until=0,next_request_at=0)
        corruptions = [dict(queue=[],active=[],turns=[]), {**base,'cooldown_until':float('nan')},
            {**base,'active':['invalid']}, {**base,'active':[dict(id='../../foreign',owner='a'*64)]},
            {**base,'active':[dict(id='request-'+'a'*32,owner='b'*64,limits=None)]}]
        flow=self.flow('corrupt-nested');flow.root.mkdir(parents=True)
        path=flow.root/'state.json'
        for value in corruptions:
            raw=json.dumps(value)
            path.write_text(raw,encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'damaged'):
                await flow.acquire('a')
            self.assertEqual(path.read_text(encoding='utf-8'),raw)

    async def test_limit_lowering_is_rechecked_before_an_existing_waiter_starts(self):
        settings={'concurrency':2}
        flow=RequestFlow(connection_id('http://127.0.0.1/v1','lower-live'),lambda:settings,self.root)
        first=await flow.acquire('a');second=await flow.acquire('b')
        waiting=asyncio.create_task(flow.acquire('c'))
        try:
            await asyncio.sleep(.06)
            settings['concurrency']=1
            await second.release()
            await asyncio.sleep(.1)
            self.assertFalse(waiting.done(),'an old cached limit cannot authorize a new request')
            await first.release()
            await (await asyncio.wait_for(waiting,2)).release()
        finally:
            await first.release();await second.release()
            if waiting.done() and not waiting.cancelled() and waiting.exception() is None:
                await waiting.result().release()
            else: waiting.cancel()
            await asyncio.gather(waiting,return_exceptions=True)

    async def test_clock_rollback_does_not_prevent_reclaiming_a_dead_process(self):
        flow=self.flow('clock')
        state=dict(queue=[],active=[dict(id='request-'+'a'*32,owner='b'*64)],turns=[],
            cooldown_until=0,next_request_at=0,pruned_at=time.time()+60)
        flow.prune(state)
        self.assertEqual(state['active'],[])

    async def test_stream_holds_slot_until_close_and_transport_failure_releases(self):
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"fixture"
        flow = self.flow()
        client_type = http_client_type(httpx.AsyncClient, flow, "a")
        async with client_type(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Body()))) as client:
            response = await client.send(client.build_request("POST", "http://127.0.0.1"), stream=True)
            waiting = asyncio.create_task(self.flow().acquire("b")); await asyncio.sleep(.08)
            self.assertFalse(waiting.done())
            await response.aclose()
            await (await asyncio.wait_for(waiting, 3)).release()
        async def failed(request):
            raise httpx.ConnectError("fixture")
        async with client_type(transport=httpx.MockTransport(failed)) as client:
            with self.assertRaises(httpx.ConnectError):
                await client.get("http://127.0.0.1")
        await (await asyncio.wait_for(flow.acquire("c"), 2)).release()

    async def test_429_does_not_replay_and_shares_retry_after(self):
        calls = []
        async def response(request):
            calls.append(time.monotonic())
            return httpx.Response(429 if len(calls) == 1 else 200, headers={"retry-after": ".2"})
        client_type = http_client_type(httpx.AsyncClient, self.flow(), "a")
        async with client_type(transport=httpx.MockTransport(response)) as client:
            self.assertEqual((await client.post("http://127.0.0.1")).status_code, 429)
            self.assertEqual(len(calls), 1, "admission never retries a possibly side-effecting request")
            await client.post("http://127.0.0.1")
        self.assertGreaterEqual(calls[1] - calls[0], .18)

    async def test_cross_process_capacity_and_crash_release(self):
        code = """import asyncio,sys
from xuloom.core.request_flow import RequestFlow
async def main():
 p=await RequestFlow(sys.argv[1],{'concurrency':1},sys.argv[2]).acquire('other-process')
 print('READY',flush=True)
 await asyncio.sleep(60)
asyncio.run(main())
"""
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        process = subprocess.Popen([sys.executable, "-u", "-c", code, connection_id("http://127.0.0.1/v1", "fixture"), str(self.root)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        waiting = None
        try:
            self.assertEqual((await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), 10)).strip(), b"READY")
            waiting = asyncio.create_task(self.flow(concurrency=2).acquire("this-process")); await asyncio.sleep(.15)
            self.assertFalse(waiting.done())
            process.terminate(); await asyncio.to_thread(process.wait, 5)
            await (await asyncio.wait_for(waiting, 3)).release()
        finally:
            if waiting and not waiting.done():
                waiting.cancel(); await asyncio.gather(waiting, return_exceptions=True)
            if process.poll() is None:
                process.terminate(); await asyncio.to_thread(process.wait, 5)
            process.stdout.close(); process.stderr.close()

    async def test_corrupt_queue_fails_closed_and_invalid_limits_rejected(self):
        flow = self.flow(); flow.root.mkdir(parents=True)
        path = flow.root / "state.json"; path.write_text('{"fixture":"corrupt"}')
        with self.assertRaises(ValueError):
            await flow.acquire("a")
        self.assertEqual(path.read_text(), '{"fixture":"corrupt"}')
        for value in ({"concurrency":0}, {"requests_per_minute":float('inf')}, [], {"unknown":1}):
            with self.assertRaises(ValueError): limits(value)
