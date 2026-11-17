"""MiniMax native media against an owned local service; never paid generation."""
import asyncio
import io
import json
import os
import subprocess
import sys
from pathlib import Path
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
from unittest.mock import AsyncMock, patch
import uuid
import wave

from xuloom.media import adapters, config, jobs
from tests import test_tool_recovery as recovery


class MiniMaxTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await recovery.RecoveryTests.asyncSetUp(self)
        self.config_patch = patch("xuloom.core.config.CONFIG_FILE", self.root / "media-config.json")
        self.config_patch.start()
        audio = io.BytesIO()
        with wave.open(audio, "wb") as out:
            out.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
            out.writeframes(b"\0\0" * 800)
        self.wav = audio.getvalue()
        self.video = b"\0\0\0\x18ftypmp42" + b"\0" * 12 + b"\0\0\0\x10mdat" + b"\0" * 8 + b"\0\0\0\x08moov"
        self.state = "queued"
        self.scenario = ""
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass

            def send(self, value, code=200, mime="application/json"):
                body = json.dumps(value).encode() if isinstance(value, dict) else value
                self.send_response(code)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try: self.wfile.write(body)
                except OSError: pass

            def note(self, body=None):
                owner.requests.append((self.command, self.path, self.headers.get("Authorization"), body))

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.note(body)
                if owner.scenario == "no-quota":
                    self.send({"error": {"message": "OWNED_SECRET"}}, 402)
                    return
                if owner.scenario == "lost":
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                if self.path == "/v1/t2a_v2":
                    value = {"base_resp": {"status_code": 0}, "data": {"status": 2, "audio": owner.wav.hex()}}
                    if owner.scenario == "rejected":
                        value = {"base_resp": {"status_code": 1004, "status_msg": "OWNED_SECRET must not echo"}, "data": None}
                    elif owner.scenario == "bad-audio":
                        value["data"]["audio"] = b"<html>invalid</html>".hex()
                    self.send(value)
                elif self.path == "/v2/video_generation":
                    self.send({"task_id": "task_owned"})
                else: self.send({"error": "wrong native endpoint"}, 404)

            def do_GET(self):
                self.note()
                if self.path == "/v2/query/video_generation/task_owned":
                    self.send({"task": {"id": "wrong" if owner.scenario == "wrong-id" else "task_owned",
                        "model": "MiniMax-H3", "status": owner.state, "content": {
                            "url": "http://127.0.0.1:1/untrusted" if owner.scenario == "foreign" else owner.base + "/asset"}}})
                elif self.path == "/asset": self.send(owner.video, mime="video/mp4")
                else: self.send({}, 404)

            def do_DELETE(self):
                self.note()
                self.send({"task_id": "task_owned", "action": "deleted", "status": "deleted"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)
        self.engine.activate_tools(["media_tool"])
        self.engine.permission_manager.approval_policy.set_mode("full")

    async def asyncTearDown(self):
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()
        self.thread.join(3)
        self.config_patch.stop()
        await recovery.RecoveryTests.asyncTearDown(self)

    def add(self, kind):
        before = {row["id"] for row in config.public_models()}
        rows = config.save_model({"kind": kind, "base_url": self.base + ("/v1/t2a_v2" if kind == "speech" else "/v2/video_generation"),
            "apiKey": "OWNED_SECRET", "model": "speech-2.8-hd" if kind == "speech" else "MiniMax-H3"})
        return next(row for row in rows if row["id"] not in before)

    async def call(self, row=None, action="submit", **extra):
        values = {"action": action, "wait_ms": 0, **({"connection": row["connectionId"], "model": row["id"], "prompt": "Owned fixture"} if row else {}), **extra}
        return await self.engine._execute_tool_wrapper({"id": uuid.uuid4().hex, "name": "media_tool", "input": values}, lambda _: None)

    def latest(self):
        return jobs.listing(self.engine.tool_context)[0]

    async def test_official_origin_detects_native_without_discovery_or_credentials(self):
        for kind in ("video", "speech"):
            binding = {"protocol": "auto", "base_url": "https://api.minimax.cn/v1", "model": {"kind": kind}}
            with patch.object(adapters, "public_schema", new=AsyncMock(side_effect=AssertionError("No discovery"))):
                self.assertEqual(await adapters.detect(binding), ("minimax-media", None))

    async def test_speech_native_hex_is_saved_as_playable_audio(self):
        result = await self.call(self.add("speech"), inputs={"voice": "Chinese (Mandarin)_Lyrical_Voice", "speed": 1.2})
        self.assertFalse(result["is_error"], result)
        self.assertEqual(Path(self.latest()["files"][0]["path"]).read_bytes(), self.wav)
        post, = [r for r in self.requests if r[0] == "POST"]
        self.assertEqual(post[1], "/v1/t2a_v2")
        self.assertEqual(post[3]["voice_setting"]["speed"], 1.2)
        self.assertEqual(post[3]["output_format"], "hex")
        self.assertEqual(post[3]["audio_setting"]["format"], "wav")
        self.assertFalse(post[3]["stream"])

    async def test_invalid_native_controls_never_submit(self):
        speech = self.add("speech")
        result = await self.call(speech, inputs={"speed": 3})
        self.assertTrue(result["is_error"])
        video = self.add("video")
        result = await self.call(video, inputs={"duration_seconds": 4.5})
        self.assertTrue(result["is_error"])
        result = await self.call(video, inputs={"seed": 1})
        self.assertTrue(result["is_error"])
        self.assertFalse(any(r[0] == "POST" for r in self.requests))

    async def test_video_submits_once_then_queries_same_saved_task(self):
        row = self.add("video")
        result = await self.call(row, inputs={"duration_seconds": 5, "aspect_ratio": "9:16"})
        self.assertFalse(result["is_error"], result)
        record = self.latest()
        self.assertEqual(record["phase"], "submitted")
        self.assertEqual(record["request_id"], "task_owned")
        body = next(r[3] for r in self.requests if r[0] == "POST")
        self.assertEqual(body["content"], [{"type": "text", "text": "Owned fixture"}])
        self.assertEqual((body["duration"], body["resolution"], body["ratio"]), (5, "768P", "9:16"))
        self.state = "succeeded"
        result = await self.call(action="status", job_id=record["id"])
        self.assertFalse(result["is_error"], result)
        self.assertEqual(Path(self.latest()["files"][0]["path"]).read_bytes(), self.video)
        self.assertEqual(sum(r[0] == "POST" for r in self.requests), 1)
        self.assertIsNone(next(r[2] for r in self.requests if r[1] == "/asset"))
        self.assertNotIn("OWNED_SECRET", json.dumps(jobs.load(self.engine.tool_context, record["id"])))

    async def test_unknown_submission_is_not_repeated(self):
        row = self.add("video")
        self.scenario = "lost"
        await self.call(row)
        record = self.latest()
        self.assertEqual(record["phase"], "unknown")
        await self.call(row)
        await self.call(action="status", job_id=record["id"])
        self.assertEqual(sum(r[0] == "POST" for r in self.requests), 1)

    async def test_saved_audio_retries_only_local_publication(self):
        row = self.add("speech")
        with patch("xuloom.media.files.publish", side_effect=OSError("owned disk failure")):
            result = await self.call(row)
        self.assertTrue(result["is_error"])
        result = await self.call(action="status", job_id=self.latest()["id"])
        self.assertFalse(result["is_error"], result)
        self.assertEqual(sum(r[0] == "POST" for r in self.requests), 1)

    async def test_http_200_provider_error_is_failure_without_echo(self):
        self.scenario = "rejected"
        result = await self.call(self.add("speech"))
        self.assertTrue(result["is_error"])
        self.assertNotIn("OWNED_SECRET", json.dumps(result))
        self.assertEqual(self.latest()["phase"], "failed")

    async def test_wrong_identity_and_foreign_asset_cannot_complete(self):
        await self.call(self.add("video"))
        record = self.latest()
        self.state, self.scenario = "succeeded", "wrong-id"
        result = await self.call(action="status", job_id=record["id"])
        self.assertTrue(result["is_error"])
        self.assertFalse(any(r[1] == "/asset" for r in self.requests))
        self.scenario = "foreign"
        result = await self.call(action="status", job_id=record["id"])
        self.assertTrue(result["is_error"])
        self.assertNotEqual(self.latest()["phase"], "downloaded")

    async def test_cancel_does_not_race_into_deleting_completed_provider_task(self):
        await self.call(self.add("video"))
        record = self.latest()
        result = await self.call(action="cancel", job_id=record["id"])
        self.assertTrue(result["is_error"])
        self.assertFalse(any(r[0] == "DELETE" for r in self.requests))
        self.state = "succeeded"
        result = await self.call(action="status", job_id=record["id"])
        self.assertFalse(result["is_error"], result)

    async def test_payment_rejection_is_not_an_unknown_submission(self):
        self.scenario = "no-quota"
        result = await self.call(self.add("video"))
        self.assertTrue(result["is_error"])
        self.assertEqual(self.latest()["phase"], "failed")
        self.assertNotIn("OWNED_SECRET", json.dumps(result))

    async def test_formal_cli_resumes_native_video_without_chat_model(self):
        with patch("xuloom.core.config.CONFIG_FILE", self.root / "config.json"):
            await self.call(self.add("video"))
            record = self.latest()
            self.state = "succeeded"
            self.engine.persist_session_state()
            self.engine.session_manager.release_writer()
            env = {**os.environ, "XULOOM_CONFIG_DIR": str(self.root), "PYTHONIOENCODING": "utf-8"}
            process = await asyncio.to_thread(subprocess.run, [sys.executable, "-m", "xuloom", "media", "status",
                "--cwd", str(self.root), "--session", self.engine.session_id, record["id"], "--permission", "full", "--json"],
                env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
            events = [json.loads(line) for line in process.stdout.splitlines()]
            result, = [event["result"] for event in events if event.get("type") == "final"]
            self.assertTrue(result["generation_completed"], result)
            self.assertEqual(sum(r[0] == "POST" for r in self.requests), 1)

    async def test_official_video_cdn_never_trusted_for_other_apis(self):
        from xuloom.media.queue import download_url
        binding = {"base_url": "https://api.minimax.cn/v2/video_generation", "download_origins": []}
        url = "https://cdn.hailuoai.com/owned.mp4"
        self.assertEqual(download_url(binding, url), url)
        with self.assertRaises(ValueError):
            download_url({**binding, "base_url": "https://unrelated.example/v2/video_generation"}, url)

    async def test_invalid_audio_is_not_published(self):
        self.scenario = "bad-audio"
        result = await self.call(self.add("speech"))
        self.assertTrue(result["is_error"])
        self.assertEqual(self.latest()["phase"], "download_failed")


if __name__ == "__main__":
    unittest.main()
