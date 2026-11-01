"""Real loopback HTTP and durable files; no paid generation or external traffic."""
import asyncio
import io
import json
import os
from pathlib import Path
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import uuid
import wave
import sys
from unittest.mock import patch
import unittest

from PIL import Image
from xuloom.core.engine import QueryEngine
from xuloom.core.run_result import PersistenceError
from xuloom.features.session_summary import read_summary
from xuloom.media import config, files, jobs, queue
from xuloom.tools.media_tool import MediaInput
from tests import test_tool_recovery as recovery


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await recovery.RecoveryTests.asyncSetUp(self)
        self.config_patch = patch("xuloom.core.config.CONFIG_FILE", self.root / "media-config.json")
        self.config_patch.start()
        self.requests = []
        self.provider_jobs = {}
        self.scenario = "normal"
        self.entered, self.release = threading.Event(), threading.Event()
        image = io.BytesIO(); Image.new("RGB", (12, 10), "blue").save(image, "PNG")
        self.png = image.getvalue()
        audio = io.BytesIO()
        with wave.open(audio, "wb") as output:
            output.setparams((1, 2, 8000, 0, "NONE", "not compressed")); output.writeframes(b"\0\0" * 800)
        self.wav = audio.getvalue()
        # Container contract only; the UI test creates and decodes real VP8/WebM.
        self.video = b"\0\0\0\x18ftypmp42" + b"\0" * 12 + b"\0\0\0\x10mdat" + b"\0" * 8 + b"\0\0\0\x08moov"
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def send(self, value, status=200, mime="application/json"):
                value = json.dumps(value).encode() if isinstance(value, dict) else value
                self.send_response(status); self.send_header("Content-Type", mime); self.send_header("Content-Length", str(len(value))); self.end_headers()
                try: self.wfile.write(value)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError): pass
            def do_POST(self):
                value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fixture.requests.append({"method": "POST", "path": self.path, "auth": self.headers.get("Authorization"), "body": value})
                if fixture.scenario == "unauthorized": self.send({"detail": "no"}, 401); return
                if fixture.scenario == "validation": self.send({"detail": [{"loc": ["body", "lyrics"], "msg": "Required field MEDIA_ONLY_SECRET", "input": "DO_NOT_ECHO_THIS_INPUT"}]}, 422); return
                identity = str(uuid.uuid4()); fixture.provider_jobs[identity] = value
                if fixture.scenario in {"lost", "hold"}:
                    fixture.entered.set()
                    if fixture.scenario == "hold": fixture.release.wait(10)
                    self.connection.shutdown(socket.SHUT_RDWR); self.connection.close(); return
                base = fixture.base + "/q/fal-ai/app/requests/" + identity
                self.send({"request_id": identity, "status": "IN_QUEUE", "status_url": ("http://127.0.0.1:1/requests/" + identity + "/status") if fixture.scenario == "foreign" else base + "/status",
                    "response_url": base, "cancel_url": base + "/cancel"})
            def do_PUT(self):
                fixture.requests.append({"method": "PUT", "path": self.path, "auth": self.headers.get("Authorization")})
                self.send({"status": "OK" if fixture.scenario == "confirmed-cancel" else "CANCELLATION_REQUESTED"})
            def do_GET(self):
                fixture.requests.append({"method": "GET", "path": self.path, "auth": self.headers.get("Authorization")})
                if self.path.startswith("/asset/"):
                    kind = self.path.rsplit("/", 1)[-1]
                    body, mime = (fixture.png, "image/png") if kind in {"image", "image-edit"} else (fixture.video, "video/mp4") if kind == "video" else (fixture.wav, "audio/wav")
                    if fixture.scenario == "bad-mime": mime = "application/javascript"
                    self.send(body, mime=mime); return
                if self.path.endswith("/status"):
                    identity = self.path.split("/")[-2]
                    state = "IN_PROGRESS" if fixture.scenario == "pending" else "COMPLETED"
                    self.send({"request_id": identity, "status": state,
                        **({"error": "Provider failed MEDIA_ONLY_SECRET", "error_type": "test_failure"} if fixture.scenario == "failed" else {})}); return
                identity = self.path.rsplit("/", 1)[-1]
                value = fixture.provider_jobs[identity]
                kind = value.get("fixture_kind", "image")
                descriptor = {"url": fixture.base + "/asset/" + kind}
                self.send({"images": [descriptor]} if kind in {"image", "image-edit"} else {"video" if kind == "video" else "audio": descriptor})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)
        models = [{"id": kind, "kind": kind, "model": "fal-ai/app/" + kind,
            "defaults": {"fixture_kind": kind}} for kind in sorted(config.KINDS)]
        self.connection = {"id": "media", "name": "Local fixture", "base_url": self.base + "/q", "apiKey": "MEDIA_ONLY_SECRET", "models": models}
        config.save(self.connection)
        self.engine.activate_tools(["media_tool"])
        from xuloom.core.messages import create_user_message
        self.engine.messages.append(create_user_message("Generate actual media", msg_uuid="media-turn")); self.engine._sync_message_context()
        self.engine.permission_manager.approval_policy.set_mode("full")

    async def asyncTearDown(self):
        self.release.set()
        await asyncio.to_thread(self.server.shutdown); self.server.server_close(); self.thread.join(2)
        self.config_patch.stop()
        await recovery.RecoveryTests.asyncTearDown(self)

    async def call(self, action="submit", **values):
        inputs = {"action": action, "wait_ms": 0, **({"connection": "media", "model": "image", "prompt": "An actual fixture image"} if action == "submit" else {}), **values}
        receipt = await self.engine._execute_tool_wrapper({"id": str(uuid.uuid4()), "name": "media_tool", "input": inputs}, lambda _: None)
        return receipt

    def latest(self):
        return jobs.listing(self.engine.tool_context)[0]

    async def test_all_media_kinds_share_one_key_actual_output_and_source_index(self):
        source = self.root / "reference.png"; source.write_bytes(self.png)
        for kind in sorted(config.KINDS):
            receipt = await self.call(model=kind, **({"image_paths": [str(source)]} if kind == "image-edit" else {}))
            self.assertFalse(receipt["is_error"], receipt)
        posts = [row for row in self.requests if row["method"] == "POST"]
        self.assertEqual(len(posts), 6)
        self.assertTrue(all(row["auth"] == "Key MEDIA_ONLY_SECRET" for row in posts))
        self.assertTrue(all(not row["auth"] for row in self.requests if row["path"].startswith("/asset/")))
        speech = next(row for row in posts if row["body"]["fixture_kind"] == "speech")
        self.assertIn("text", speech["body"]); self.assertNotIn("prompt", speech["body"])
        edit = next(row for row in posts if row["body"]["fixture_kind"] == "image-edit")
        self.assertTrue(edit["body"]["image_urls"][0].startswith("data:image/png;base64,"))
        summary = read_summary(self.root, self.engine.session_id)
        self.assertEqual(len(summary["files"]), 6)
        for file in summary["files"]: self.assertTrue(Path(file["path"]).is_file())
        self.assertTrue(all(row["phase"] == "downloaded" for row in jobs.listing(self.engine.tool_context)))
        for file in jobs.directory(self.engine.tool_context).glob("*.json"):
            self.assertNotIn("MEDIA_ONLY_SECRET", file.read_text())

    async def test_capabilities_and_shared_key_config_are_masked_and_keep_chat_settings(self):
        cfg = config.load_config(); cfg["model_provider"] = "keep-me"
        from xuloom.core.config import save_config
        save_config(cfg)
        value = config.public_connections()[0]
        self.assertTrue(value["hasKey"]); self.assertNotIn("MEDIA_ONLY_SECRET", json.dumps(value)); self.assertEqual(len(value["models"]), 6)
        config.save({**self.connection, "apiKey": ""})
        self.assertEqual(config.connections()[0]["api_key"], "MEDIA_ONLY_SECRET")
        receipt = await self.call("capabilities")
        self.assertFalse(receipt["is_error"]); self.assertEqual(self.requests, [])
        self.assertNotIn("MEDIA_ONLY_SECRET", str(receipt))
        config.remove("media"); self.assertEqual(config.load_config()["model_provider"], "keep-me")

    async def test_ask_plan_and_explicit_denial_block_paid_submissions(self):
        self.engine.permission_manager.approval_policy.set_mode("ask")
        self.assertTrue((await self.call())["is_error"]); self.assertEqual(self.requests, [])
        self.engine.permission_manager.approval_policy.set_mode("full"); self.engine.plan_manager.set_mode("plan")
        self.assertTrue((await self.call())["is_error"]); self.assertEqual(self.requests, [])
        self.engine.plan_manager.set_mode("normal"); self.engine.permission_manager.always_deny_tools.add("media_tool")
        self.assertTrue((await self.call())["is_error"]); self.assertEqual(self.requests, [])

    async def test_unknown_accepted_submission_is_not_automatically_reposted_after_restart(self):
        self.scenario = "lost"
        self.assertTrue((await self.call())["is_error"])
        self.assertEqual(self.latest()["phase"], "unknown")
        self.assertTrue((await self.call())["is_error"])
        self.assertEqual(len(self.provider_jobs), 1)
        identity = self.engine.session_id
        self.engine.session_manager.release_writer()
        restored = QueryEngine(model="fixture", model_provider="openai", cwd=str(self.root))
        try:
            self.assertTrue(restored.load_session(identity)); restored.permission_manager.approval_policy.set_mode("full")
            restored.activate_tools(["media_tool"])
            result = await restored._execute_tool_wrapper({"id": "new-process-call", "name": "media_tool", "input": {
                "action": "submit", "connection": "media", "model": "image", "prompt": "An actual fixture image", "wait_ms": 0}}, lambda _: None)
            self.assertTrue(result["is_error"]); self.assertEqual(len(self.provider_jobs), 1)
        finally:
            restored.session_manager.release_writer(); await restored.client.close(); await restored.compactor.client.close()

    async def test_retry_unknown_still_requires_real_approval_under_full_access(self):
        self.scenario = "lost"; await self.call()
        requests = []
        async def deny(request): requests.append(request); return "deny"
        self.engine.set_permission_handler(deny)
        self.assertTrue((await self.call(retry_unknown=True))["is_error"])
        self.assertEqual(len(requests), 1); self.assertEqual(len(self.provider_jobs), 1)

    async def test_cancel_request_is_not_reported_as_completed_and_status_does_not_repost(self):
        self.scenario = "pending"; await self.call()
        row = self.latest(); self.assertEqual(row["phase"], "submitted")
        result = await self.call("cancel", job_id=row["id"])
        self.assertFalse(result["is_error"]); self.assertEqual(self.latest()["phase"], "cancel_requested")
        self.scenario = "normal"; result = await self.call("status", job_id=row["id"])
        self.assertFalse(result["is_error"]); self.assertEqual(self.latest()["phase"], "downloaded")
        self.assertEqual(len(self.provider_jobs), 1)

    async def test_provider_completed_with_error_is_not_success_and_has_no_output(self):
        self.scenario = "failed"; result = await self.call()
        self.assertTrue(result["is_error"]); self.assertEqual(self.latest()["phase"], "failed")
        self.assertNotIn("MEDIA_ONLY_SECRET", str(result)); self.assertNotIn("MEDIA_ONLY_SECRET", self.latest()["error"])
        self.assertEqual(read_summary(self.root, self.engine.session_id)["files"], [])

    async def test_confirmed_cancel_is_a_successful_control_not_a_finished_generation(self):
        self.scenario = "pending"; await self.call()
        row = self.latest(); self.scenario = "confirmed-cancel"
        result = await self.call("cancel", job_id=row["id"])
        self.assertFalse(result["is_error"])
        self.assertEqual(self.latest()["phase"], "cancelled")
        self.assertIn('"finished": false', result["content"])
        before = len(self.requests)
        result = await self.call("cancel", job_id=row["id"])
        self.assertFalse(result["is_error"]); self.assertEqual(len(self.requests), before)
        self.assertTrue((await self.call("status", job_id=row["id"]))["is_error"])
        self.assertEqual(read_summary(self.root, self.engine.session_id)["files"], [])

    async def test_foreign_queue_url_never_receives_credentials(self):
        self.scenario = "foreign"; result = await self.call()
        self.assertTrue(result["is_error"]); self.assertEqual(self.latest()["phase"], "unknown")
        self.assertEqual(len(self.requests), 1)

    async def test_bad_mime_and_oversized_media_are_not_saved_as_completed(self):
        self.scenario = "bad-mime"; result = await self.call()
        self.assertTrue(result["is_error"]); self.assertEqual(self.latest()["phase"], "download_failed")
        self.assertEqual(list((self.root / "generated").glob("*")), [])
        self.scenario = "normal"
        with patch.object(files, "IMAGE_LIMIT", 16):
            result = await self.call(prompt="Different output")
        self.assertTrue(result["is_error"]); self.assertEqual(list((self.root / "generated").glob("*")), [])

    async def test_intent_save_failure_blocks_post_and_late_save_failure_preserves_uncertainty(self):
        with patch.object(jobs, "save", side_effect=OSError("disk full")):
            with self.assertRaises(PersistenceError): await self.call()
        self.assertEqual(self.requests, [])
        self.engine.in_progress_tools.clear()
        original = jobs.save
        def fail_after_submit(context, record):
            if record["phase"] != "submitting": raise OSError("disk full after POST")
            return original(context, record)
        with patch.object(jobs, "save", side_effect=fail_after_submit):
            with self.assertRaises(PersistenceError): await self.call(prompt="Known one POST")
        self.assertEqual(len(self.provider_jobs), 1); self.assertEqual(self.latest()["phase"], "submitting")

    async def test_local_cancel_keeps_unknown_receipt_instead_of_replaying_generation(self):
        self.scenario = "hold"
        task = asyncio.create_task(self.call())
        self.assertTrue(await asyncio.to_thread(self.entered.wait, 5)); task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(self.latest()["phase"], "unknown"); self.assertEqual(len(self.provider_jobs), 1)

    async def test_changed_connection_and_other_agent_cannot_control_old_job(self):
        self.scenario = "pending"; await self.call(); row = self.latest()
        config.save({**self.connection, "base_url": self.base + "/different"})
        before = len(self.requests)
        self.assertTrue((await self.call("status", job_id=row["id"]))["is_error"])
        self.assertEqual(len(self.requests), before)
        context = {**self.engine.tool_context, "agent_id": "sibling", "parent_agent_id": self.engine.agent_id}
        with self.assertRaisesRegex(ValueError, "another agent"): jobs.load(context, row["id"])

    async def test_discovery_input_validation_and_api_boundaries(self):
        search = await self.engine.available_tools["tool_search_tool"].execute(query="生成图片 视频 音乐")
        self.assertIn("media_tool", str(search))
        for value in ({"action": "submit"}, {"action": "status"}, {"action": "list", "prompt": "bad"}, {"action": "submit", "connection": "media", "model": "image", "prompt": "ok", "wait_ms": 120001}):
            with self.assertRaises(ValueError): MediaInput(**value)
        for url in ("https://user:password@example.com", "https://example.com/#fragment", "javascript:alert(1)", "http://example.com\\host",
                    "https://example.com/q/../admin", "https://example.com/q/%2e%2e/admin", "https://example.com/q/%2Fadmin", "https://example.com/q//admin"):
            with self.assertRaises(ValueError): config.address(url)
        binding, key = config.resolve("media", "image")
        self.assertEqual(key, "MEDIA_ONLY_SECRET")
        for tail in ("/../admin", "/%2e%2e/admin", "/%2fadmin"):
            with self.assertRaises(ValueError): queue.queue_url(binding, binding["base_url"] + tail)
        with self.assertRaises(ValueError): queue.download_url(binding, "http://127.0.0.1:1/private")
        with self.assertRaises(ValueError): config.save({**self.connection, "models": [{"id": "bad", "kind": "image", "model": "fal-ai/app", "defaults": {"api_key": "NO"}}]})

    async def test_approved_target_cannot_be_changed_while_user_is_answering(self):
        self.engine.permission_manager.approval_policy.set_mode("ask")
        prompts = []
        async def approve(request):
            prompts.append(request.summary)
            config.save({**self.connection, "base_url": self.base + "/changed"})
            return "allow"
        self.engine.set_permission_handler(approve)
        result = await self.call()
        self.assertTrue(result["is_error"], result)
        self.assertIn(self.base + "/q/fal-ai/app/image", prompts[0])
        self.assertIn("changed while waiting", result["content"])
        self.assertEqual(self.requests, [])

    async def test_corrupt_receipt_is_visible_and_blocks_an_automatic_new_submission(self):
        self.scenario = "lost"; await self.call(); row = self.latest()
        path = jobs.directory(self.engine.tool_context) / (row["id"] + ".json")
        path.write_text("{incomplete", encoding="utf-8")
        self.assertEqual(self.latest()["phase"], "unreadable")
        result = await self.call(); self.assertTrue(result["is_error"])
        self.assertIn("unreadable", result["content"]); self.assertEqual(len(self.provider_jobs), 1)

    async def test_fork_does_not_copy_ownership_and_move_keeps_original_output_location(self):
        from xuloom.core.session_mutation import mutate
        self.scenario = "pending"; await self.call(); row = self.latest()
        sid = self.engine.session_id; self.engine.session_manager.release_writer()
        target = self.root / "target"; target.mkdir()
        spec = {"project": str(self.root), "target": str(target), "session_id": sid, "scope": "project", "title": "copy", "created": 123}
        mutate({**spec, "action": "fork", "new_id": "fork-media"})
        self.assertEqual(jobs.listing({"session_cwd": str(target), "session_id": "fork-media"}), [])
        mutate({**spec, "action": "move"})
        moved = jobs.load({"session_cwd": str(target), "session_id": sid}, row["id"])
        self.assertEqual(moved["output_dir"], str(self.root / "generated"))
        self.assertEqual(moved["request_id"], row["request_id"])

    async def test_media_preview_reads_ranges_and_refuses_changed_or_external_files(self):
        from xuloom.media import preview
        await self.call(model="speech")
        path = Path(self.latest()["files"][0]["path"])
        info = preview.info(self.root, str(path), [])
        self.assertEqual(info["mime"], "audio/wav")
        part = preview.read(self.root, str(path), [], info["version"], 7, 30)
        import base64
        self.assertEqual(base64.b64decode(part["data"]), self.wav[7:37]); self.assertEqual(part["nextOffset"], 37)
        for offset, length in ((-1, 10), (0, 524289), (True, 1)):
            with self.assertRaises(ValueError): preview.read(self.root, str(path), [], info["version"], offset, length)
        path.write_bytes(self.wav + b"changed")
        with self.assertRaisesRegex(ValueError, "版本"): preview.read(self.root, str(path), [], info["version"])
        external = self.root.parent / ("outside-" + uuid.uuid4().hex + ".wav")
        try:
            external.write_bytes(self.wav)
            with self.assertRaises(ValueError): preview.info(self.root, str(external), [])
        finally: external.unlink(missing_ok=True)

    async def test_formal_cli_uses_same_config_without_key_echo_or_model_requests(self):
        value = dict(self.connection); value.pop("apiKey")
        path = self.root / "new-connection.json"; path.write_text(json.dumps({**value, "id": "from-cli"}), encoding="utf-8")
        env = {**os.environ, "XULOOM_CONFIG_DIR": str(self.root / "config-cli"), "MEDIA_TEST_KEY": "FROM_ENV_ONLY", "OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": ""}
        async def command(*args):
            process = await asyncio.create_subprocess_exec(sys.executable, "-m", "xuloom", "media", *args, "--json", env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(process.communicate(), 30)
            self.assertEqual(process.returncode, 0, err.decode()); self.assertNotIn(b"FROM_ENV_ONLY", out)
            return json.loads(out)["result"]
        result = await command("save", "--file", str(path), "--api-key-env", "MEDIA_TEST_KEY")
        self.assertTrue(result["connections"][0]["hasKey"])
        self.assertEqual(len((await command("connections"))["connections"]), 1)
        self.assertEqual((await command("remove", "from-cli", "--yes"))["connections"], [])
        self.assertEqual(self.requests, [])

    async def test_existing_output_conflict_never_overwrites_user_file(self):
        self.scenario = "pending"; await self.call(); row = self.latest()
        folder = self.root / "generated"; folder.mkdir(exist_ok=True)
        target = folder / (row["id"] + "-1.png"); target.write_bytes(b"USER_OWNED_CONTENT")
        self.scenario = "normal"
        result = await self.call("status", job_id=row["id"])
        self.assertTrue(result["is_error"]); self.assertEqual(target.read_bytes(), b"USER_OWNED_CONTENT")
        self.assertEqual(self.latest()["phase"], "download_failed")

    async def test_header_only_video_is_not_treated_as_a_generated_media_payload(self):
        for name, data in (("empty.webm", b"\x1aE\xdf\xa3webm" + b"\0" * 100), ("empty.mp4", b"\0\0\0\x18ftypmp42" + b"\0" * 80)):
            path = self.root / name; path.write_bytes(data)
            with self.assertRaises(ValueError): files.identify(path, "video")

    async def test_rejected_provider_input_explains_fields_without_echoing_credentials_or_input(self):
        for scenario in ("unauthorized", "validation"):
            self.scenario = scenario
            result = await self.call(prompt="A " + scenario + " example")
            self.assertTrue(result["is_error"]); self.assertEqual(self.latest()["phase"], "failed")
            self.assertNotIn("MEDIA_ONLY_SECRET", str(result)); self.assertNotIn("DO_NOT_ECHO_THIS_INPUT", str(result))
            if scenario == "validation": self.assertIn("body.lyrics", str(result))
        self.assertEqual(self.provider_jobs, {})

    async def test_formal_status_and_cancel_do_not_need_a_chat_key_or_invoke_a_model(self):
        self.scenario = "pending"; await self.call(); first = self.latest()
        await self.call(prompt="A separate pending request"); second = self.latest()
        self.engine.session_manager.release_writer()
        configuration = self.root / "control-config"; configuration.mkdir()
        (configuration / "config.json").write_text(json.dumps(config.load_config()), encoding="utf-8")
        env = {**os.environ, "XULOOM_CONFIG_DIR": str(configuration), "OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": "",
            "OPENAI_MODEL": "", "ANTHROPIC_MODEL": "", "OPENAI_BASE_URL": self.base + "/chat/v1", "ANTHROPIC_BASE_URL": self.base + "/chat"}
        async def command(action, identity, *extra):
            process = await asyncio.create_subprocess_exec(sys.executable, "-m", "xuloom", "media", action, identity,
                "--cwd", str(self.root), "--session", self.engine.session_id, "--json", *extra,
                env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(process.communicate(), 30)
            self.assertNotIn(b"MEDIA_ONLY_SECRET", out)
            events = [json.loads(line) for line in out.splitlines()]
            return process.returncode, events[-1]["result"], err.decode()
        before = len(self.requests)
        code, value, error = await command("status", first["id"])
        self.assertEqual(code, 3, error); self.assertEqual(value["reason"], "approval_required"); self.assertEqual(len(self.requests), before)
        self.scenario = "normal"
        code, value, error = await command("status", first["id"], "--permission", "full")
        self.assertEqual(code, 0, error); self.assertTrue(value["generation_completed"])
        code, value, error = await command("cancel", second["id"], "--permission", "full")
        self.assertEqual(code, 0, error); self.assertFalse(value["generation_completed"])
        self.assertEqual(value["job"]["phase"], "cancel_requested")
        self.scenario = "confirmed-cancel"
        code, value, error = await command("cancel", second["id"], "--permission", "full")
        self.assertEqual(code, 0, error); self.assertFalse(value["generation_completed"])
        self.assertEqual(value["reason"], "completed"); self.assertEqual(value["job"]["phase"], "cancelled")
        self.assertEqual(len(self.provider_jobs), 2)
        self.assertTrue(all(row["path"].startswith(("/q/", "/asset/")) for row in self.requests))
