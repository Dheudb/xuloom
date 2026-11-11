import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from xuloom import EngineConfig
from xuloom.models import ToolCall, model_response
from xuloom.server import XuloomServer
from xuloom.testing import ScriptedModel

TOKEN = "test-token-0123456789"


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "root"
        (self.root / "project").mkdir(parents=True)
        self.env = patch.dict(os.environ, {"XULOOM_DATA_DIR": str(Path(self.temp.name) / "data"),
                                           "HOME": str(Path(self.temp.name) / "home"),
                                           "USERPROFILE": str(Path(self.temp.name) / "home")})
        self.env.start()
        self.model = ScriptedModel().install()
        config = self.model.config(EngineConfig(collaboration_mode="solo"))
        self.server = XuloomServer(config, root=self.root, token=TOKEN, port=0, approval_timeout=30)
        host, port = await self.server.start()
        self.serving = asyncio.create_task(self.server.serve_forever())
        self.client = httpx.AsyncClient(base_url=f"http://{host}:{port}/v1", headers={"Authorization": "Bearer " + TOKEN},
                                        timeout=30, trust_env=False)

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.server.close()
        self.serving.cancel()
        await asyncio.gather(self.serving, return_exceptions=True)
        self.model.uninstall()
        self.env.stop()
        self.temp.cleanup()

    async def session(self, **body):
        response = await self.client.post("/sessions", json=body)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    async def test_authentication(self):
        anonymous = httpx.AsyncClient(base_url=self.client.base_url, trust_env=False)
        try:
            self.assertEqual((await anonymous.get("/health")).json(), {"ok": True})
            self.assertEqual((await anonymous.get("/sessions")).status_code, 401)
            wrong = await anonymous.get("/sessions", headers={"Authorization": "Bearer " + TOKEN + "x"})
            self.assertEqual(wrong.status_code, 401)
        finally:
            await anonymous.aclose()
        with self.assertRaises(ValueError):
            XuloomServer(root=self.root, token="short")

    async def test_blocking_message_returns_the_final_result(self):
        self.model.script = [model_response(text="hello there", input_tokens=5, output_tokens=2)]
        session = await self.session(cwd="project")
        self.assertEqual(Path(session["cwd"]), (self.root / "project").resolve())
        response = await self.client.post(f"/sessions/{session['session_id']}/messages", json={"text": "hi"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["text"], "hello there")
        self.assertTrue(response.json()["result"]["ok"])
        state = (await self.client.get(f"/sessions/{session['session_id']}")).json()
        self.assertEqual(state["usage"]["input_tokens"], 5)
        self.assertFalse(state["running"])

    async def test_streamed_run_with_approval(self):
        self.model.script = [
            model_response(tool_calls=[ToolCall("c1", "file_write_tool", {"file_path": "a.txt", "content": "approved"})]),
            model_response(tool_calls=[ToolCall("c2", "file_write_tool", {"file_path": "b.txt", "content": "denied"})]),
            model_response(text="finished"),
        ]
        session = await self.session(cwd="project", permission="ask")
        sid = session["session_id"]
        events, decisions = [], iter(["allow", "deny"])
        async with self.client.stream("POST", f"/sessions/{sid}/messages", json={"text": "write", "stream": True}) as response:
            self.assertEqual(response.headers["content-type"].split(";")[0], "text/event-stream")
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[6:])
                events.append(event)
                if event["type"] == "approval_required":
                    self.assertEqual(event["tool_name"], "file_write_tool")
                    pending = (await self.client.get(f"/sessions/{sid}")).json()["pending"]
                    self.assertEqual([row["request_id"] for row in pending], [event["request_id"]])
                    answer = await self.client.post(f"/sessions/{sid}/approvals/{event['request_id']}",
                                                    json={"decision": next(decisions)})
                    self.assertEqual(answer.status_code, 200, answer.text)
        self.assertEqual(events[-1]["type"], "final")
        self.assertEqual(events[-1]["text"], "finished")
        self.assertEqual((self.root / "project" / "a.txt").read_text(), "approved")
        self.assertFalse((self.root / "project" / "b.txt").exists())
        self.assertEqual(sum(1 for event in events if event["type"] == "approval_required"), 2)

    async def test_validation_and_lifecycle(self):
        for body, status in (({"cwd": "../outside"}, 400), ({"cwd": "missing"}, 400), ({"permission": "root"}, 400),
                             ({"extra": 1}, 400)):
            response = await self.client.post("/sessions", json=body)
            self.assertEqual(response.status_code, status, (body, response.text))
        session = await self.session()
        sid = session["session_id"]
        self.assertEqual((await self.client.post(f"/sessions/{sid}/messages", json={})).status_code, 400)
        self.assertEqual((await self.client.post(f"/sessions/{sid}/approvals/nope", json={"decision": "allow"})).status_code, 404)
        self.assertEqual((await self.client.post(f"/sessions/{sid}/messages", content=b"{bad")).status_code, 400)
        gate = asyncio.Event()

        async def slow(request):
            await gate.wait()
            return model_response(text="late")
        self.model.script = [slow]
        first = asyncio.create_task(self.client.post(f"/sessions/{sid}/messages", json={"text": "one"}))
        for _ in range(100):
            if (await self.client.get(f"/sessions/{sid}")).json()["running"]:
                break
            await asyncio.sleep(0.02)
        busy = await self.client.post(f"/sessions/{sid}/messages", json={"text": "two"})
        self.assertEqual(busy.status_code, 409)
        gate.set()
        self.assertEqual((await first).json()["text"], "late")
        self.assertEqual(len((await self.client.get("/sessions")).json()["sessions"]), 1)
        self.assertEqual((await self.client.delete(f"/sessions/{sid}")).status_code, 204)
        self.assertEqual((await self.client.get(f"/sessions/{sid}")).status_code, 404)
        self.assertEqual((await self.client.get("/nope")).status_code, 404)

    async def test_saved_session_can_be_reopened_once(self):
        self.model.script = [model_response(text="first")]
        sid = (await self.session(cwd="project"))["session_id"]
        await self.client.post(f"/sessions/{sid}/messages", json={"text": "hi"})
        self.assertEqual((await self.client.post("/sessions", json={"cwd": "project", "session_id": sid})).status_code, 409)
        await self.client.delete(f"/sessions/{sid}")
        reopened = await self.session(cwd="project", session_id=sid)
        self.assertEqual(reopened["session_id"], sid)
        self.assertEqual((await self.client.post("/sessions", json={"cwd": "project", "session_id": "missing"})).status_code, 404)


if __name__ == "__main__":
    unittest.main()
