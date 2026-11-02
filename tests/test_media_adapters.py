"""Three-field setup and actual owned HTTP media, without paid/provider traffic."""
import asyncio
import base64
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import socket
import sys
import threading
import unittest
from unittest.mock import patch
import uuid
import wave

from PIL import Image
from xuloom.media import adapters, config, jobs
from tests import test_tool_recovery as recovery


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await recovery.RecoveryTests.asyncSetUp(self)
        self.config_patch = patch("xuloom.core.config.CONFIG_FILE", self.root / "generation-config.json"); self.config_patch.start()
        self.requests, self.scenario = [], "normal"
        image = io.BytesIO(); Image.new("RGB", (16, 12), "blue").save(image, "PNG"); self.png = image.getvalue()
        audio = io.BytesIO()
        with wave.open(audio, "wb") as out:
            out.setparams((1, 2, 8000, 0, "NONE", "not compressed")); out.writeframes(b"\0\0" * 800)
        self.wav = audio.getvalue()
        self.video = b"\0\0\0\x18ftypmp42" + b"\0" * 12 + b"\0\0\0\x10mdat" + b"\0" * 8 + b"\0\0\0\x08moov"
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def send(self, value, status=200, mime="application/json"):
                body = json.dumps(value).encode() if isinstance(value, dict) else value
                self.send_response(status); self.send_header("Content-Type", mime); self.send_header("Content-Length", str(len(body))); self.end_headers()
                try: self.wfile.write(body)
                except (OSError, ConnectionError): pass
            def note(self, payload=None):
                owner.requests.append({"method": self.command, "path": self.path, "auth": self.headers.get("Authorization"),
                    "xi": self.headers.get("xi-api-key"), "payload": payload})
            def do_GET(self):
                self.note()
                if self.path.endswith("/openapi.json"):
                    self.send({"detail": "not available"}, 404); return
                if self.path.endswith("/voices"):
                    self.send({"voices": [{"voice_id": "OWNED_VOICE"}]}); return
                if self.path.endswith("/videos/vid_owned/content"):
                    self.send(owner.video, mime="video/mp4"); return
                if self.path.endswith("/videos/vid_owned"):
                    self.send({"id": "other" if owner.scenario == "wrong-id" else "vid_owned",
                        "status": "queued" if owner.scenario == "pending" else "failed" if owner.scenario == "video-failed" else "completed"}); return
                if self.path == "/asset/image":
                    self.send(owner.png, mime="image/png"); return
                self.send({"detail": "not found"}, 404)
            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                mime = self.headers.get("Content-Type", "")
                if mime.startswith("multipart/"):
                    message = BytesParser(policy=default).parsebytes(("Content-Type: " + mime + "\r\n\r\n").encode() + raw)
                    payload = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True) if part.get_filename() else part.get_content().strip() for part in message.iter_parts()}
                else: payload = json.loads(raw)
                self.note(payload)
                if owner.scenario == "lost":
                    self.connection.shutdown(socket.SHUT_RDWR); self.connection.close(); return
                if owner.scenario == "bad-request": self.send({"error": {"message": "OWNED_SECRET must not echo"}}, 400); return
                if self.path.endswith(("/images/generations", "/images/edits")):
                    if owner.scenario == "foreign-url": self.send({"data": [{"url": "http://127.0.0.1:1/forbidden"}]}); return
                    if owner.scenario == "url": self.send({"data": [{"url": owner.base + "/asset/image"}]}); return
                    self.send({"data": [{"b64_json": base64.b64encode(owner.png).decode()}]}); return
                if self.path.endswith("/videos"):
                    self.send({"id": "vid_owned", "status": "queued"}); return
                if "/audio/speech" in self.path or "/text-to-speech/" in self.path or self.path.endswith(("/sound-generation", "/music")):
                    self.send(b"<html>bad</html>" if owner.scenario == "bad-bytes" else owner.wav, mime="audio/wav"); return
                self.send({"error": "unknown endpoint"}, 404)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler); self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)
        self.engine.activate_tools(["media_tool"]); self.engine.permission_manager.approval_policy.set_mode("full")

    async def asyncTearDown(self):
        await asyncio.to_thread(self.server.shutdown); self.server.server_close(); self.thread.join(3)
        self.config_patch.stop()
        await recovery.RecoveryTests.asyncTearDown(self)

    def add(self, kind="image", suffix="/v1", model="owned-model"):
        before = {(m["connectionId"], m["id"]) for m in config.public_models()}
        rows = config.save_model({"kind": kind, "base_url": self.base + suffix, "apiKey": "OWNED_SECRET", "model": model})
        return next(m for m in rows if (m["connectionId"], m["id"]) not in before)

    async def call(self, row=None, action="submit", **extra):
        values = {"action": action, "wait_ms": 0, **({"connection": row["connectionId"], "model": row["id"], "prompt": "Generate owned fixture"} if row else {}), **extra}
        return await self.engine._execute_tool_wrapper({"id": uuid.uuid4().hex, "name": "media_tool", "input": values}, lambda _: None)

    def latest(self): return jobs.listing(self.engine.tool_context)[0]

    async def test_five_kinds_three_fields_masked_key_and_shared_storage(self):
        for kind in ["image", "video", "speech", "sound", "music"]: self.add(kind)
        self.assertEqual(len(config.public_models()), 5)
        self.assertEqual(len(config.connections()), 1)
        self.assertEqual(self.requests, [], "Saving must not call any service")
        self.assertNotIn("OWNED_SECRET", json.dumps(config.public_models()))
        self.assertTrue(all(row["hasKey"] for row in config.public_models()))
        speech = next(m for m in config.public_models() if m["kind"] == "speech")
        config.save_model({**{k:v for k,v in speech.items() if k != "hasKey"}, "apiKey": ""})
        self.assertEqual(config.connections()[0]["api_key"], "OWNED_SECRET")

    async def test_editing_one_shared_model_does_not_change_sibling_target_or_key(self):
        first, other = self.add(), self.add("video")
        updated = config.save_model({"connectionId": first["connectionId"], "id": first["id"], "kind": "image", "base_url": self.base + "/different",
            "model": "new-name", "apiKey": "DIFFERENT_KEY"})
        unchanged = next(m for m in updated if m["id"] == other["id"])
        self.assertEqual(unchanged, other)
        self.assertEqual(config.resolve(other["connectionId"], other["id"])[1], "OWNED_SECRET")

    async def test_standard_image_b64_and_reference_edit_publish_real_files(self):
        row = self.add()
        result = await self.call(row, inputs={"size": "1024x1024"})
        self.assertFalse(result["is_error"], result)
        image = self.root / "source.png"; image.write_bytes(self.png)
        edit = await self.call(row, image_paths=[str(image)])
        self.assertFalse(edit["is_error"], edit)
        posts = [r for r in self.requests if r["method"] == "POST"]
        self.assertEqual([r["path"] for r in posts], ["/v1/images/generations", "/v1/images/edits"])
        self.assertEqual(posts[0]["payload"], {"model": "owned-model", "prompt": "Generate owned fixture", "n": 1, "size": "1024x1024"})
        self.assertEqual(posts[1]["payload"]["image[]"], self.png)
        self.assertTrue(all(r["auth"] == "Bearer OWNED_SECRET" for r in posts))
        self.assertTrue(all(not r["auth"] for r in self.requests if r["path"].endswith("openapi.json")))
        for record in jobs.listing(self.engine.tool_context):
            self.assertEqual(record["phase"], "downloaded"); self.assertEqual(Path(record["files"][0]["path"]).read_bytes(), self.png)

    async def test_full_endpoint_is_not_duplicated_and_bad_parameter_never_posts(self):
        row = self.add(suffix="/v1/images/generations")
        failed = await self.call(row, inputs={"unknown_json": "do not send"})
        self.assertTrue(failed["is_error"]); self.assertEqual(self.requests, [])
        okay = await self.call(row)
        self.assertFalse(okay["is_error"], okay)
        self.assertEqual(self.requests[0]["path"], "/v1/images/generations")

    async def test_speech_sound_music_have_protocol_specific_inputs_and_playable_audio(self):
        for kind, suffix in [("speech", "/v1/audio/speech"), ("sound", "/v1/sound-generation"), ("music", "/v1/music")]:
            row = self.add(kind, suffix)
            result = await self.call(row, inputs={"duration_seconds": 5} if kind != "speech" else {})
            self.assertFalse(result["is_error"], result)
            self.assertEqual(Path(self.latest()["files"][0]["path"]).read_bytes(), self.wav)
        posts = [r for r in self.requests if r["method"] == "POST"]
        self.assertEqual(posts[0]["payload"]["input"], "Generate owned fixture")
        self.assertEqual(posts[0]["payload"]["voice"], "alloy")
        self.assertEqual(posts[1]["payload"]["model_id"], "owned-model"); self.assertEqual(posts[1]["payload"]["duration_seconds"], 5)
        self.assertEqual(posts[2]["payload"]["music_length_ms"], 5000)
        self.assertEqual(posts[2]["xi"], "OWNED_SECRET"); self.assertIsNone(posts[2]["auth"])

    async def test_speech_service_resolves_a_real_available_voice_without_user_json(self):
        row = self.add("speech", "/v1/text-to-speech")
        result = await self.call(row)
        self.assertFalse(result["is_error"], result)
        self.assertEqual(self.requests[0]["path"], "/v1/voices")
        self.assertEqual(self.requests[-1]["path"], "/v1/text-to-speech/OWNED_VOICE")

    async def test_video_is_submitted_once_polling_and_saved_content_are_bound_to_job(self):
        row = self.add("video")
        self.scenario = "pending"
        submitted = await self.call(row, inputs={"duration_seconds": 8})
        self.assertFalse(submitted["is_error"], submitted)
        record = self.latest(); self.assertEqual(record["phase"], "submitted")
        self.scenario = "normal"
        done = await self.call(action="status", job_id=record["id"])
        self.assertFalse(done["is_error"], done)
        self.assertEqual(Path(self.latest()["files"][0]["path"]).read_bytes(), self.video)
        self.assertEqual(len([r for r in self.requests if r["method"] == "POST"]), 1)
        self.assertEqual([r for r in self.requests if r["method"] == "POST"][0]["payload"]["seconds"], "8")

    async def test_lost_sync_response_is_unknown_and_identical_request_does_not_resubmit(self):
        row = self.add(); self.scenario = "lost"
        result = await self.call(row); self.assertTrue(result["is_error"])
        record = self.latest(); self.assertEqual(record["phase"], "unknown")
        await self.call(row)
        await self.call(action="status", job_id=record["id"])
        self.assertEqual(len([r for r in self.requests if r["method"] == "POST"]), 1)
        self.assertNotIn("OWNED_SECRET", json.dumps(jobs.load(self.engine.tool_context, record["id"])))

    async def test_invalid_audio_and_foreign_download_are_not_success(self):
        row = self.add("speech", "/v1/audio/speech"); self.scenario = "bad-bytes"
        result = await self.call(row); self.assertTrue(result["is_error"])
        self.assertEqual(self.latest()["phase"], "download_failed")
        self.scenario = "foreign-url"; row = self.add()
        result = await self.call(row); self.assertTrue(result["is_error"])
        self.assertEqual(self.latest()["phase"], "download_failed")

    async def test_saved_sync_result_can_be_republished_without_generation_after_disk_failure(self):
        row = self.add()
        with patch("xuloom.media.files.publish", side_effect=OSError("owned disk failure")):
            result = await self.call(row)
        self.assertTrue(result["is_error"])
        record = self.latest()
        done = await self.call(action="status", job_id=record["id"])
        self.assertFalse(done["is_error"], done)
        self.assertEqual(len([r for r in self.requests if r["method"] == "POST"]), 1)

    async def test_unsupported_audio_root_and_permission_denial_never_probe_generation(self):
        row = self.add("music")
        result = await self.call(row); self.assertTrue(result["is_error"])
        self.assertFalse(any(r["method"] == "POST" for r in self.requests))
        self.requests.clear(); self.engine.permission_manager.always_deny_tools.add("media_tool")
        result = await self.call(row); self.assertTrue(result["is_error"]); self.assertEqual(self.requests, [])

    async def test_wrong_video_identity_and_unsupported_cancel_do_not_delete_or_resubmit(self):
        row = self.add("video"); self.scenario = "pending"
        await self.call(row); record = self.latest(); self.scenario = "wrong-id"
        result = await self.call(action="status", job_id=record["id"]); self.assertTrue(result["is_error"])
        result = await self.call(action="cancel", job_id=record["id"]); self.assertTrue(result["is_error"])
        self.assertEqual(len([r for r in self.requests if r["method"] == "POST"]), 1)
        self.assertFalse(any(r["method"] == "DELETE" for r in self.requests))

    async def test_formal_cli_can_add_model_without_json_or_key_in_arguments(self):
        folder = self.root / "cli-settings"; folder.mkdir()
        env = {**os.environ, "XULOOM_CONFIG_DIR": str(folder), "OWNED_MEDIA_KEY": "CLI_PRIVATE_KEY",
            "PYTHONPATH": os.environ.get("XULOOM_TEST_SITE") or str(Path(__file__).resolve().parents[1])}
        process = await asyncio.create_subprocess_exec(sys.executable, "-m", "xuloom", "media", "add", "--kind", "image",
            "--api-url", self.base + "/v1", "--model", "owned-model", "--api-key-env", "OWNED_MEDIA_KEY", "--json",
            cwd=self.root, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
        self.assertEqual(process.returncode, 0, stderr.decode()); self.assertNotIn(b"CLI_PRIVATE_KEY", stdout + stderr)
        result = json.loads(stdout.decode().splitlines()[-1])["result"]
        self.assertEqual(result["models"][0]["model"], "owned-model"); self.assertEqual(self.requests, [])

    async def test_fal_schema_maps_supported_fields_without_saved_parameter_json(self):
        binding = {"model": {"kind": "speech", "model": "owner/voice"}}
        schema = {"paths": {"/run": {"post": {"requestBody": {"content": {"application/json": {"schema": {
            "properties": {"text": {}, "voice_id": {"default": "SCHEMA_VOICE"}}, "required": ["text", "voice_id"]}}}}}}}}
        payload = await adapters.fal_payload(binding, "hello", {}, [], schema)
        self.assertEqual(payload, {"text": "hello", "voice_id": "SCHEMA_VOICE"})

    async def test_video_reference_stays_on_video_endpoint_and_contains_actual_image(self):
        row = self.add("video", "/v1/videos")
        image = self.root / "reference.png"; image.write_bytes(self.png)
        result = await self.call(row, image_paths=[str(image)])
        self.assertFalse(result["is_error"], result)
        post = next(r for r in self.requests if r["method"] == "POST")
        self.assertEqual(post["path"], "/v1/videos")
        self.assertEqual(post["payload"]["input_reference"], self.png)

    async def test_known_queue_uses_public_schema_without_credentials_or_generation_probe(self):
        binding = {"protocol": "auto", "base_url": "https://queue.fal.run", "model": {"kind": "image", "model": "owner/custom-model"}}
        with patch("xuloom.media.adapters.public_schema", return_value={"paths": {}}) as schema:
            protocol, _ = await adapters.detect(binding)
        self.assertEqual(protocol, "fal-queue")
        self.assertEqual(schema.call_args.args[1], "https://fal.ai/api/openapi/queue/openapi.json?endpoint_id=owner%2Fcustom-model")
        self.assertEqual(self.requests, [])

    async def test_wrong_full_audio_type_and_edit_without_reference_never_post(self):
        music = self.add("music", "/v1/sound-generation")
        denied = await self.call(music); self.assertTrue(denied["is_error"])
        image = self.add("image", "/v1/images/edits")
        denied = await self.call(image); self.assertTrue(denied["is_error"])
        self.assertEqual(self.requests, [])
