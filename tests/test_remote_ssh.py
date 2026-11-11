"""System OpenSSH + encrypted loopback SSH + real remote bridge and CLI."""
import asyncio
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
import wave
from unittest.mock import patch

if __package__:
    from . import ssh_fixture, test_runtime_connections as local
else:
    import ssh_fixture
    import test_runtime_connections as local
from xuloom.remote.config import validate_profile, save, profiles, remove
from xuloom.remote.ssh import SSHClient, RemoteDisconnected, RemoteRequestError, ssh_argv
from xuloom.remote.cli import dispatch as remote_dispatch
from xuloom.remote.proxy import SSHBridge
from xuloom.remote.terminal import interactive_chat, PipeInput
from rich.console import Console


class SSHConfigTests(unittest.TestCase):
    def test_profile_roundtrip_and_no_credential_fields_or_shell_options(self):
        with tempfile.TemporaryDirectory() as directory, patch("xuloom.core.config.CONFIG_FILE", Path(directory)/"config.json"):
            value = {"id": "lab", "host": "user@host", "cwd": "/tmp/work", "python": "python3"}
            save(value); self.assertEqual(profiles()[0]["id"], "lab")
            for update in ({"host": "-oProxyCommand=bad"}, {"host": "host;echo bad"}, {"cwd": "relative"}, {"port": True}, {"api_key": "not-allowed"}, {"python": "x\ny"}, {"platform": []}, {"ssh_config": "relative_config"}):
                with self.assertRaises(ValueError): validate_profile({**value, **update})
            args = ssh_argv(value)
            self.assertIn("StrictHostKeyChecking=yes", args); self.assertIn("BatchMode=yes", args)
            self.assertIn("ControlPath=none", args)
            self.assertEqual(args[-2], "user@host")
            remove("lab"); self.assertEqual(profiles(), [])


class SSHProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_pipe_input_retains_utf8_and_can_cancel_an_idle_read_without_a_thread(self):
        incoming, outgoing = os.pipe()
        try:
            with os.fdopen(incoming, "rb", buffering=0) as stream:
                reader = PipeInput(stream)
                waiting = asyncio.create_task(reader.read())
                await asyncio.sleep(.06); waiting.cancel()
                with self.assertRaises(asyncio.CancelledError): await waiting
                os.write(outgoing, "你好\n/quit\n".encode("utf-8"))
                self.assertEqual(await reader.read(), "你好"); self.assertEqual(await reader.read(), "/quit")
        finally: os.close(outgoing)

    async def test_timeout_does_not_replay_request(self):
        writes = []
        class Input:
            def write(self, value): writes.append(value)
            async def drain(self): pass
        client = SSHClient({"id": "a", "host": "alias", "cwd": "/tmp"})
        client.process = SimpleNamespace(stdin=Input())
        with self.assertRaisesRegex(RemoteDisconnected, "unconfirmed"):
            await client.request("prompt", {"text": "one effect"}, timeout=.01)
        self.assertEqual(len(writes), 1); self.assertEqual(client.pending, {})


@unittest.skipUnless(ssh_fixture.paramiko is not None and shutil.which("ssh"), "dev-only paramiko and system OpenSSH are required")
class SSHWireTests(unittest.IsolatedAsyncioTestCase):
    async def test_reference_inspection_uses_remote_scope_without_model_requests(self):
        sid = await self.empty_chat()
        client = await self.connect()
        await client.request("initialize", {"sessionId": sid})
        result = await client.request("files.reference", {"path": "note.txt"})
        self.assertEqual(Path(result["path"]), self.remote.resolve()/"note.txt")
        self.assertFalse(result["directory"])
        self.assertTrue((await client.request("files.reference", {"path": "."}))["directory"])
        with self.assertRaises(RemoteRequestError):
            await client.request("files.reference", {"path": "../outside.txt"})
        self.assertEqual(self.local.records, [])

    async def asyncSetUp(self):
        self.local = local.RuntimeConnectionTests(); await self.local.asyncSetUp()
        self.root = self.local.root
        self.remote = self.root / "remote & '科学"; self.remote.mkdir()
        (self.remote/"note.txt").write_text("remote source 你好", encoding="utf-8")
        remote_config = self.root/"remote-config"; remote_config.mkdir()
        (remote_config/"config.json").write_text(json.dumps({"active_model_connection": "remote", "model_connections": [{"id": "remote", "provider": "openai", "model": "opaque-model", "models": ["opaque-model"], "api_key": "key-remote", "base_url": self.local.base+"/remote/v1"}]}), encoding="utf-8")
        environment = {key: value for key,value in os.environ.items() if not key.startswith(("OPENAI_", "ANTHROPIC_", "XULOOM_"))}
        environment.update(XULOOM_CONFIG_DIR=str(remote_config), XULOOM_DATA_DIR=str(self.root/"remote-data"), XULOOM_SKILLS_PATHS="", HOME=str(self.root/"remote-home"), USERPROFILE=str(self.root/"remote-home"))
        self.server = await asyncio.to_thread(ssh_fixture.SSHFixture, self.root, environment)
        self.profile = validate_profile({"id": "test", "host": "loopback-fixture", "cwd": str(self.remote), "python": os.environ.get("XULOOM_TEST_REMOTE_PYTHON", sys.executable),
            "platform": "windows" if os.name == "nt" else "posix", "ssh_config": str(self.server.config)})
        self.clients = []
        self.release = None

    async def asyncTearDown(self):
        if self.release: self.release.set()
        for client in self.clients:
            try: await client.aclose()
            except (OSError, ValueError): pass
        await asyncio.to_thread(self.server.close)
        await self.local.asyncTearDown()

    async def connect(self, profile=None, callback=None):
        client = SSHClient(profile or self.profile, callback); self.clients.append(client)
        await client.start(); return client

    async def test_remote_media_ranges_use_remote_bytes_without_a_model_request(self):
        memory = io.BytesIO()
        with wave.open(memory, "wb") as output:
            output.setparams((1, 2, 8000, 0, "NONE", "not compressed")); output.writeframes(b"\0\0" * 800)
        raw = memory.getvalue(); path = self.remote / "remote-audio.wav"; path.write_bytes(raw)
        client = await self.connect()
        before = len(self.local.records)
        info = await client.request("files.mediaInfo", {"path": str(path)})
        part = await client.request("files.mediaChunk", {"path": str(path), "version": info["version"], "offset": 11, "length": 51})
        import base64
        self.assertEqual(base64.b64decode(part["data"]), raw[11:62])
        self.assertEqual(info["mime"], "audio/wav"); self.assertEqual(len(self.local.records), before)
        with self.assertRaises(RemoteRequestError):
            await client.request("files.mediaChunk", {"path": str(path), "version": "old", "offset": 0, "length": 51})

    def hold_model(self, *, question=False, write=False):
        started, self.release = threading.Event(), threading.Event()
        owner = self
        def respond(handler):
            body = json.loads(handler.rfile.read(int(handler.headers["Content-Length"])))
            owner.local.records.append({"path": handler.path, "headers": dict(handler.headers), "body": body})
            if not body.get("stream"):
                handler.send_response(200); handler.send_header("Content-Type", "application/json"); handler.end_headers()
                handler.wfile.write(json.dumps({"choices": [{"message": {"role": "assistant", "content": "memory"}}]}).encode()); return
            delta = {"content": "partial remote"}
            question_turn = question and body["messages"][-1]["role"] != "tool"
            write_turn = write and body["messages"][-1]["role"] != "tool"
            if question_turn:
                delta = {"tool_calls": [{"index": 0, "id": "question", "type": "function", "function": {"name": "ask_user_question_tool", "arguments": json.dumps({"questions": [{"question": "Choose input", "options": [{"label": "A"}, {"label": "B"}]}]})}}]}
            if write_turn:
                delta = {"tool_calls": [{"index": 0, "id": "write", "type": "function", "function": {"name": "file_write_tool", "arguments": json.dumps({"file_path": str(owner.remote/"must-not-exist.txt"), "content": "DENIED_WRITE"})}}]}
            handler.send_response(200); handler.send_header("Content-Type", "text/event-stream"); handler.end_headers()
            def frame(value, finish=None): return ("data: "+json.dumps({"choices": [{"index": 0, "delta": value, "finish_reason": finish}]})+"\n\n").encode()
            try:
                handler.wfile.write(frame(delta)); handler.wfile.flush(); started.set()
                if not question and not write: owner.release.wait(30)
                handler.wfile.write(frame({}, "tool_calls" if question_turn or write_turn else "stop")+b"data: [DONE]\n\n")
            except (OSError, ConnectionError): pass
        self.local.server.RequestHandlerClass.do_POST = respond
        return started

    def cli_environment(self):
        config = self.root/"local-config"; config.mkdir(exist_ok=True)
        (config/"config.json").write_text(json.dumps({"ssh_connections": [self.profile], "model_connections": []}), encoding="utf-8")
        env = {**os.environ, "XULOOM_CONFIG_DIR": str(config), "XULOOM_DATA_DIR": str(self.root/"local-data"), "OPENAI_API_KEY": "never-forward-local-key", "XULOOM_SKILLS_PATHS": ""}
        return env

    async def program(self, argv, data=b""):
        process = await asyncio.create_subprocess_exec(os.environ.get("XULOOM_TEST_LOCAL_PYTHON", sys.executable), "-I", "-m", "xuloom", *argv,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=self.cli_environment())
        communication = asyncio.create_task(process.communicate(data))
        try: output, error = await asyncio.wait_for(asyncio.shield(communication), 45)
        except asyncio.TimeoutError:
            process.kill(); output, error = await communication
            self.fail("CLI did not exit: " + output.decode("utf-8", "replace") + error.decode("utf-8", "replace"))
        return process.returncode, output.decode("utf-8"), error.decode("utf-8")

    async def run_cli(self, *args):
        env = self.cli_environment()
        process = await asyncio.create_subprocess_exec(os.environ.get("XULOOM_TEST_LOCAL_PYTHON", sys.executable), "-I", "-m", "xuloom", "remote", *args, "--json", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
        communication = asyncio.create_task(process.communicate())
        try: output, error = await asyncio.wait_for(asyncio.shield(communication), 40)
        except asyncio.TimeoutError:
            process.kill()
            output, error = await communication
            self.fail("CLI did not exit:\n"+output.decode("utf-8", "replace")+"\n"+error.decode("utf-8", "replace"))
        events = [json.loads(line) for line in output.splitlines()]
        self.assertEqual(sum(event["type"] == "final" for event in events), 1, error.decode()+output.decode())
        return process.returncode, events, error.decode()

    async def empty_chat(self):
        client = await self.connect(); sid = (await client.request("sessions.create"))["sessionId"]
        await client.aclose(); return sid

    async def test_attachment_cli_upload_listing_download_and_prompt_use_remote_copies(self):
        import hashlib
        from PIL import Image
        sid = await self.empty_chat()
        source = self.root / 'local paper.pdf'; content = b'%PDF-fixture' * 70000; source.write_bytes(content)
        before = len(self.local.records)
        code, events, error = await self.run_cli('attach', 'test', str(source), '--session', sid)
        self.assertEqual(code, 0, error + str(events))
        item = events[-1]['result']['attachments'][0]; identity = item['id']
        self.assertEqual(item['sha256'], hashlib.sha256(content).hexdigest())
        self.assertNotIn(str(source), item['path'])
        source.unlink()
        code, events, error = await self.run_cli('attachments', 'test', '--session', sid)
        self.assertEqual(code, 0, error); self.assertEqual(events[-1]['result']['attachments']['items'][0]['id'], identity)
        destination = self.root / 'download.pdf'
        code, events, error = await self.run_cli('download', 'test', identity, str(destination), '--session', sid)
        self.assertEqual(code, 0, error); self.assertEqual(destination.read_bytes(), content)
        self.assertEqual(len(self.local.records), before, 'Transfer and listing must not invoke a model')
        image = self.root / 'pixels.png'; Image.new('RGB', (30, 20), 'red').save(image)
        code, events, error = await self.run_cli('exec', 'test', 'Read attachment pixels', '--session', sid, '--attach', str(image), '--attachment', identity)
        self.assertEqual(code, 0, error + str(events))
        request = next(record['body'] for record in reversed(self.local.records) if record['body'].get('stream'))
        user = next(row for row in reversed(request['messages']) if row['role'] == 'user')
        self.assertTrue(any(block.get('type') == 'image_url' for block in user['content']))
        self.assertIn(item['path'].replace('\\', '/'), str(user))
        self.assertNotIn(str(source), str(user)); self.assertFalse((self.root / 'local-data' / 'history').exists())

    async def test_interactive_attachment_selection_survives_separate_readonly_download(self):
        sid = await self.empty_chat(); source = self.root / 'local notes.txt'; source.write_text('independent copy', encoding='utf-8')
        commands = iter(['/attach ' + str(source), '/attachments', 'Review attached notes', '/attachments clear', '/quit'])
        output = io.StringIO()
        async def read(_): return next(commands)
        with patch('xuloom.remote.proxy.resolve', return_value=self.profile):
            result = await interactive_chat('test', session_id=sid, read=read, console=Console(file=output))
        self.assertTrue(result['ok'], result); self.assertIn('Next message attachments:', output.getvalue())
        client = await self.connect(); initial = await client.request('initialize', {'sessionId': sid})
        user = next(row for row in initial['history'] if row['role'] == 'user')
        self.assertEqual(user['text'], 'Review attached notes'); self.assertEqual(len(user['attachments']), 1)
        self.assertNotEqual(user['attachments'][0]['path'], str(source))

    async def test_real_cli_links_and_terminal_resume_use_remote_history_and_never_a_local_engine(self):
        sid = await self.empty_chat()
        connections = len(self.server.commands)
        code, output, error = await self.program(["remote", "chat", "test", "--json"])
        self.assertEqual(code, 2); self.assertEqual(len(self.server.commands), connections)
        code, output, error = await self.program(["chat-link", "--remote", "test", "--session", sid, "--json"])
        self.assertEqual(code, 0, error+output)
        result = json.loads(output)["result"]; url = result["url"]
        self.assertTrue(result["chat"]["remote_verified"]); self.assertEqual(self.local.records, [])
        connections = len(self.server.commands)
        code, output, error = await self.program(["chat-link", "--resolve", url, "--json"])
        self.assertEqual(code, 0, error); self.assertFalse(json.loads(output)["result"]["chat"]["remote_verified"])
        self.assertEqual(len(self.server.commands), connections, "Resolving a URI must not launch SSH")
        code, output, error = await self.program(["--chat", url], "/status\n/link\n你好 CLI\n/quit\n".encode("utf-8"))
        self.assertEqual(code, 0, error+output); self.assertIn(url, output); self.assertIn("answer-remote", output)
        self.assertNotIn("key-remote", output); self.assertNotIn("never-forward-local-key", output)
        client = await self.connect(); history = (await client.request("initialize", {"sessionId": sid}))["history"]
        self.assertEqual([row["text"] for row in history if row["role"] == "user"], ["你好 CLI"])
        self.assertFalse((self.root/"local-data/history").exists())
        for record in self.local.records: self.local.assert_credentials(record, "remote", "openai")
        code, output, error = await self.program(["remote", "chat", "test", "--session", sid], b"/quit\n")
        self.assertEqual(code, 0, error+output); self.assertIn("你好 CLI", output)

    async def test_interactive_questions_wait_for_real_answers_and_model_controls_stay_remote(self):
        self.hold_model(question=True)
        sid = await self.empty_chat(); inputs = iter(["/model list", "/model use 1", "/permission full", "$web-research ask me", "2", "/quit"])
        output = io.StringIO()
        async def read(_): return next(inputs)
        registry_readers = []
        def terminal(registry):
            registry_readers.append(registry)
            async def prompt(label):
                from prompt_toolkit.document import Document
                from xuloom.skills.terminal import SkillCompleter
                completions = list(SkillCompleter(registry).get_completions(Document("$we"), None))
                self.assertTrue(any(choice.text == "web-research " for choice in completions))
                return await read(label)
            return SimpleNamespace(prompt_async=prompt)
        with patch("xuloom.remote.proxy.resolve", return_value=self.profile), patch.object(sys.stdin, "isatty", return_value=True), patch.object(sys.stdout, "isatty", return_value=True), patch("xuloom.skills.terminal.input_session", side_effect=terminal):
            result = await interactive_chat("test", session_id=sid, binding=self.profile, console=Console(file=output))
        self.assertTrue(result["ok"], result); self.assertIn("Choose input", output.getvalue())
        self.assertEqual(len(registry_readers), 1)
        self.assertTrue(any(record["body"]["messages"][-1]["role"] == "tool" and "B" in json.dumps(record["body"]["messages"][-1]) for record in self.local.records))
        self.assertEqual(sum(bool(r["body"].get("stream")) for r in self.local.records), 2)
        self.assertTrue(any("Skill: web-research" in json.dumps(record["body"]) for record in self.local.records))

    async def test_interactive_approval_never_defaults_to_allow_or_writes_on_deny(self):
        self.hold_model(write=True); sid = await self.empty_chat()
        inputs = iter(["write it", "", "2", "/quit"]); output = io.StringIO()
        async def read(_): return next(inputs)
        with patch("xuloom.remote.proxy.resolve", return_value=self.profile):
            result = await interactive_chat("test", session_id=sid, binding=self.profile, read=read, console=Console(file=output))
        self.assertTrue(result["ok"], result)
        self.assertFalse((self.remote/"must-not-exist.txt").exists())
        self.assertIn("空输入不会自动批准", output.getvalue())

    async def test_bad_terminal_answer_stops_owned_turn_instead_of_leaving_an_invisible_pending_approval(self):
        self.hold_model(write=True); sid = await self.empty_chat(); count = 0
        async def read(_):
            nonlocal count
            count += 1
            if count == 1: return "write it"
            if count == 2: raise ValueError("malformed terminal answer")
            return "/quit"
        with patch("xuloom.remote.proxy.resolve", return_value=self.profile):
            result = await interactive_chat("test", session_id=sid, binding=self.profile, read=read, console=Console(file=io.StringIO()))
        self.assertTrue(result["ok"], result)
        self.assertFalse((self.remote/"must-not-exist.txt").exists())
        client = await self.connect()
        self.assertEqual((await client.request("session.status", {"sessionId": sid}))["writer"], "available")
        initial = await client.request("initialize", {"sessionId": sid})
        self.assertEqual([message["text"] for message in initial["history"] if message["role"] == "user"], ["write it"])

    async def test_interactive_stop_then_continue_keeps_the_original_prompt(self):
        started = self.hold_model(); sid = await self.empty_chat(); inputs = iter(["original remote task", "/continue", "/quit"])
        async def read(_):
            value = next(inputs)
            if value == "/continue": self.release.set()
            return value
        with patch("xuloom.remote.proxy.resolve", return_value=self.profile):
            task = asyncio.create_task(interactive_chat("test", session_id=sid, binding=self.profile, read=read, console=Console(file=io.StringIO())))
            self.assertTrue(await asyncio.to_thread(started.wait, 20)); task.cancel()
            result = await asyncio.wait_for(task, 30)
        self.assertTrue(result["ok"], result)
        client = await self.connect(); initial = await client.request("initialize", {"sessionId": sid})
        self.assertEqual([row["text"] for row in initial["history"] if row["role"] == "user"], ["original remote task"])
        self.assertEqual(sum(bool(r["body"].get("stream")) for r in self.local.records), 2)

    async def test_interactive_disconnect_cancels_idle_input_without_a_pending_stdin_thread(self):
        sid = await self.empty_chat(); reading, cancelled = asyncio.Event(), asyncio.Event()
        async def read(_):
            reading.set()
            try: await asyncio.Future()
            finally: cancelled.set()
        with patch("xuloom.remote.proxy.resolve", return_value=self.profile):
            task = asyncio.create_task(interactive_chat("test", session_id=sid, binding=self.profile, read=read, console=Console(file=io.StringIO())))
            await asyncio.wait_for(reading.wait(), 20); self.server.disconnect()
            result = await asyncio.wait_for(task, 15)
        self.assertEqual(result["reason"], "remote_disconnected"); self.assertTrue(result["outcome_unknown"])
        self.assertTrue(cancelled.is_set()); self.assertEqual(self.local.records, [])

    async def test_handshake_history_files_status_and_git_need_no_model(self):
        client = await self.connect()
        self.assertEqual(Path(client.info["cwd"]), self.remote)
        self.assertEqual(client.info["transport"], "ssh")
        identity = (await client.request("sessions.create"))["sessionId"]
        initial = await client.request("initialize", {"sessionId": identity})
        self.assertEqual(initial["history"], [])
        status = await client.request("session.status", {"sessionId": identity})
        self.assertEqual(status["writer"], "available")
        self.assertEqual((await client.request("files.read", {"path": "note.txt"}))["content"], "remote source 你好")
        self.assertTrue(any(row["name"] == "note.txt" for row in (await client.request("files.list"))["entries"]))
        self.assertEqual((await client.request("git.status"))["code"], "not_repository")
        self.assertEqual(self.local.records, [])

    async def test_desktop_proxy_refuses_changed_binding_before_ssh_and_mismatched_machine_before_history(self):
        events = []
        changed = {**self.profile, "cwd": str(self.root)}
        bridge = SSHBridge("test", self.profile, None, events.append)
        with patch("xuloom.remote.proxy.resolve", return_value=changed):
            with self.assertRaisesRegex(ValueError, "目标已更改"):
                await bridge.dispatch("remote.connection", {})
        self.assertEqual(self.server.commands, [])
        bridge = SSHBridge("test", self.profile, {"hostname": "different-machine", "platform": "unknown", "cwd": str(self.remote)}, events.append)
        with patch("xuloom.remote.proxy.resolve", return_value=self.profile):
            with self.assertRaisesRegex(ValueError, "机器或目录"):
                await bridge.dispatch("initialize", {"sessionId": "never-opened"})
        self.assertIsNone(bridge.client)
        self.assertFalse((self.root/"remote-data/history").exists())
        self.assertEqual(self.local.records, [])

    async def test_formal_cli_uses_remote_key_and_history_not_local_config(self):
        async def run(*args):
            code, events, error = await self.run_cli(*args)
            self.assertEqual(code, 0, error+json.dumps(events))
            return events[-1]["result"]
        first = await run("exec", "test", "first remote question", "--permission", "full")
        self.assertTrue(first["ok"], first); self.assertEqual(first["text"], "answer-remote")
        second = await run("exec", "test", "second remote question", "--session", first["session_id"], "--permission", "full")
        self.assertEqual(second["session_id"], first["session_id"])
        generation = [record for record in self.local.records if record["body"].get("stream")]
        self.assertEqual(len(generation), 2)
        for record in self.local.records: self.local.assert_credentials(record, "remote", "openai")
        self.assertIn("first remote question", json.dumps(generation[-1]["body"]))
        self.assertFalse((self.root/"local-data/history").exists())
        self.assertTrue(any((self.root/"remote-data/history").rglob(first["session_id"]+".json")))

    async def test_summary_cli_reads_remote_receipts_without_another_model_call_or_local_history(self):
        self.hold_model(write=True)
        code, events, error = await self.run_cli("exec", "test", "Write the remote fixture", "--permission", "full")
        self.assertEqual(code, 0, error + json.dumps(events))
        identity = events[-1]["result"]["session_id"]
        before = len(self.local.records)
        code, events, error = await self.run_cli("summary", "test", "--session", identity, "--all")
        self.assertEqual(code, 0, error + json.dumps(events))
        summary = events[-1]["result"]["summary"]
        self.assertEqual(summary["files"][0]["path"], str(self.remote / "must-not-exist.txt"))
        self.assertEqual(len(self.local.records), before)
        self.assertFalse((self.root / "local-data/history").exists())

    async def test_host_key_failure_does_not_start_remote_bridge_or_trust_new_key(self):
        self.server.known_hosts.write_text("", encoding="utf-8")
        with self.assertRaises(RemoteDisconnected): await self.connect()
        self.assertEqual(self.server.commands, [])
        self.assertEqual(self.server.known_hosts.read_text(), "")

    async def test_transport_loss_does_not_replay_and_saved_chat_can_be_inspected(self):
        queue = asyncio.Queue(); client = await self.connect(callback=queue.put_nowait)
        identity = (await client.request("sessions.create"))["sessionId"]
        await client.request("initialize", {"sessionId": identity, "permissionMode": "full"})
        await client.request("prompt", {"text": "only once"})
        while True:
            event = await asyncio.wait_for(queue.get(), 20)
            if event["type"] == "turn_finished": break
        self.server.disconnect()
        while True:
            event = await asyncio.wait_for(queue.get(), 10)
            if event["type"] == "remote_disconnected": break
        self.assertTrue(event["outcome_unknown"])
        recovered = await self.connect()
        initial = await recovered.request("initialize", {"sessionId": identity})
        self.assertEqual(initial["history"][-1]["text"], "answer-remote")
        self.assertEqual(sum(bool(record["body"].get("stream")) for record in self.local.records), 1)

    async def test_active_disconnect_reports_unknown_without_replaying(self):
        started = self.hold_model(); queue = asyncio.Queue()
        client = await self.connect(callback=queue.put_nowait)
        identity = (await client.request("sessions.create"))["sessionId"]
        await client.request("initialize", {"sessionId": identity, "permissionMode": "full"})
        await client.request("prompt", {"text": "hold once"})
        self.assertTrue(await asyncio.to_thread(started.wait, 15))
        self.server.disconnect()
        while True:
            event = await asyncio.wait_for(queue.get(), 10)
            if event["type"] == "remote_disconnected": break
        self.assertTrue(event["outcome_unknown"])
        for process in self.server.processes: await asyncio.to_thread(process.wait, 10)
        recovered = await self.connect()
        self.assertEqual((await recovered.request("session.status", {"sessionId": identity}))["writer"], "available")
        initial = await recovered.request("initialize", {"sessionId": identity})
        self.assertEqual(len([m for m in initial["history"] if m["role"] == "user"]), 1)
        self.assertEqual(sum(bool(r["body"].get("stream")) for r in self.local.records), 1)

    async def test_stop_is_owned_busy_writer_rejected_and_explicit_resume_keeps_prompt(self):
        started = self.hold_model(); queue = asyncio.Queue()
        owner = await self.connect(callback=queue.put_nowait)
        identity = (await owner.request("sessions.create"))["sessionId"]
        await owner.request("initialize", {"sessionId": identity, "permissionMode": "full"})
        await owner.request("prompt", {"text": "original stopped task"})
        self.assertTrue(await asyncio.to_thread(started.wait, 15))
        other_events = asyncio.Queue(); other = await self.connect(callback=other_events.put_nowait)
        await other.request("initialize", {"sessionId": identity})
        self.assertEqual((await other.request("session.status", {"sessionId": identity}))["writer"], "elsewhere")
        await other.request("stop")
        self.assertTrue((await owner.request("session.status", {"sessionId": identity}))["busy"])
        with self.assertRaises(RemoteRequestError) as failure: await other.request("prompt", {"text": "not allowed"})
        self.assertEqual(failure.exception.code, "session_busy")
        await owner.request("stop"); await owner.aclose()
        self.release.set()
        self.assertEqual((await other.request("session.status", {"sessionId": identity}))["writer"], "available")
        await other.request("prompt.resume")
        while True:
            event = await asyncio.wait_for(other_events.get(), 20)
            if event["type"] == "turn_finished": break
        await other.aclose()
        inspect = await self.connect()
        initial = await inspect.request("initialize", {"sessionId": identity})
        self.assertEqual([m["text"] for m in initial["history"] if m["role"] == "user"], ["original stopped task"])

    async def test_noninteractive_question_blocks_without_assuming_an_answer(self):
        self.hold_model(question=True)
        code, events, error = await self.run_cli("exec", "test", "ask me", "--permission", "full")
        self.assertEqual(code, 3, error+json.dumps(events))
        self.assertEqual(events[-1]["result"]["reason"], "input_required")
        self.assertTrue(any(event["type"] == "input_required" for event in events))

    async def test_cli_cancellation_confirms_owned_stop(self):
        started = self.hold_model(); events = []
        async def emit(event): events.append(event)
        args = SimpleNamespace(action="exec", id="test", session=None, prompt="wait", resume=False, permission="full", collaboration=None, skill=[], attach=[], attachment=[])
        with patch("xuloom.remote.config.resolve", return_value=self.profile):
            task = asyncio.create_task(remote_dispatch(args, emit))
            self.assertTrue(await asyncio.to_thread(started.wait, 15))
            task.cancel(); result = await asyncio.wait_for(task, 15)
        self.assertEqual(result["reason"], "cancelled", result)
        self.assertTrue(result["stop_confirmed"]); self.assertFalse(result["outcome_unknown"])
        self.assertTrue(result["session_id"])

    async def test_two_simultaneous_prompts_have_one_owner_turn(self):
        started = self.hold_model(); client = await self.connect()
        identity = (await client.request("sessions.create"))["sessionId"]
        await client.request("initialize", {"sessionId": identity, "permissionMode": "full"})
        results = await asyncio.gather(client.request("prompt", {"text": "first"}), client.request("prompt", {"text": "second"}), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, dict) and result.get("started") for result in results), 1)
        self.assertEqual(sum(isinstance(result, RemoteRequestError) for result in results), 1)
        self.assertTrue(await asyncio.to_thread(started.wait, 15))
        await client.request("stop")
        self.assertEqual(sum(bool(r["body"].get("stream")) for r in self.local.records), 1)
