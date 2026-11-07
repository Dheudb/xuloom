"""Actual CLI subprocesses and local HTTP/SSE; never remote model traffic."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import queue
import unittest


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.requests = []
        self.wire = []
        self.mode = "text"
        self.request_started, self.finish_request = threading.Event(), threading.Event()
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                if not raw: return
                body = json.loads(raw)
                owner.wire.append({"path": self.path, "headers": dict(self.headers), "body": body})
                if owner.mode == "responses" and self.path == "/v1/responses/compact":
                    self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
                    self.wfile.write(json.dumps({"id":"compact-owned", "created_at":1, "object":"response.compaction",
                        "output":[{"type":"compaction", "id":"opaque-owned", "encrypted_content":"KEEP_CANONICAL"}],
                        "usage":{"input_tokens":4000,"output_tokens":200,"total_tokens":4200}}).encode()); return
                if owner.mode == "responses" and self.path == "/v1/responses":
                    owner.requests.append(body)
                    output = [{"type":"message", "id":"msg-owned", "role":"assistant", "status":"completed", "content":[{"type":"output_text", "text":"NATIVE_CONTEXT_OK", "annotations":[]}]}]
                    self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
                    for event in [{"type":"response.output_text.delta", "sequence_number":1, "item_id":"msg-owned", "output_index":0,"content_index":0,"delta":"NATIVE_CONTEXT_OK"},
                        {"type":"response.completed", "sequence_number":2, "response":{"id":"resp-owned", "created_at":1, "object":"response", "status":"completed", "model":"fixture", "output":output, "usage":{"input_tokens":30,"output_tokens":5,"total_tokens":35}}}]:
                        self.wfile.write(("data: "+json.dumps(event)+"\n\n").encode())
                    return
                if owner.mode == 'native-search' and self.path == '/v1/tools/search':
                    self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
                    self.wfile.write(json.dumps({'results': [{'url': 'https://evidence.invalid/paper', 'title': 'Paper', 'summary': 'LOCAL_SEARCH_EVIDENCE'}]}).encode()); return
                if not body.get("stream"):
                    self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
                    self.wfile.write(json.dumps({"choices": [{"message": {"role": "assistant", "content": "memory"}}]}).encode()); return
                owner.requests.append(body)
                owner.request_started.set()
                if owner.mode == "hold": owner.finish_request.wait(25)
                has_tool_result = body["messages"][-1]["role"] == "tool"
                delta, finish = {"content": "CLI_RESULT_你好"}, "stop"
                if owner.mode == 'native-search' and len(owner.requests) <= 2:
                    name = 'tool_search_tool' if len(owner.requests) == 1 else 'web_search_tool'
                    arguments = {'query': 'web search', 'activate': ['web_search_tool']} if len(owner.requests) == 1 else {'query': 'question', 'max_results': 15}
                    delta, finish = {'tool_calls': [{'index': 0, 'id': 'native-search-' + str(len(owner.requests)), 'type': 'function',
                        'function': {'name': name, 'arguments': json.dumps(arguments)}}]}, 'tool_calls'
                if owner.mode == "heterogeneous" and not self.path.startswith("/worker/") and not has_tool_result:
                    delta, finish = {"tool_calls": [{"index": 0, "id": "delegate", "type": "function", "function": {"name": "spawn_agent",
                        "arguments": json.dumps({"prompt": "independent evidence", "subagent_type": "other-account"})}}]}, "tool_calls"
                if owner.mode == "write" and not has_tool_result:
                    delta, finish = {"tool_calls": [{"index": 0, "id": "effect", "type": "function", "function": {"name": "file_write_tool",
                        "arguments": json.dumps({"file_path": str(owner.root/"effect.txt"), "content": "ONCE"})}}]}, "tool_calls"
                if owner.mode == "question" and not has_tool_result:
                    delta, finish = {"tool_calls": [{"index": 0, "id": "question", "type": "function", "function": {"name": "ask_user_question_tool",
                        "arguments": json.dumps({"questions": [{"question": "Choose input", "options": [{"label":"A"},{"label":"B"}]}]})}}]}, "tool_calls"
                self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
                chunks = [{"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                    {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}, {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}]
                try: self.wfile.write(("".join("data: "+json.dumps(c)+"\n\n" for c in chunks)+"data: [DONE]\n\n").encode())
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError): pass
        self.server = ThreadingHTTPServer(("127.0.0.1",0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.env = {**os.environ, "XULOOM_DATA_DIR": str(self.root/"data"), "XULOOM_CONFIG_DIR": str(self.root/"config"),
            "HOME": str(self.root/"home"), "USERPROFILE": str(self.root/"home"), "XULOOM_SKILLS_PATHS": "",
            "OPENAI_API_KEY": "fixture", "OPENAI_MODEL": "fixture", "XULOOM_MODEL_PROVIDER": "openai",
            "OPENAI_BASE_URL": f"http://127.0.0.1:{self.server.server_port}/v1", "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": os.environ.get("XULOOM_TEST_SITE", str(Path(__file__).resolve().parents[1])), "XULOOM_NETWORK_MODE": "direct"}

    def tearDown(self):
        self.finish_request.set()
        self.server.shutdown(); self.server.server_close(); self.thread.join(5); self.temp.cleanup()

    def cli(self, *args, stdin=""):
        result = subprocess.run([sys.executable, "-m", "xuloom", *args, "--json", "--cwd", str(self.root)],
            cwd=self.root, input=stdin, text=True, encoding="utf-8", capture_output=True, env=self.env, timeout=35)
        events = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(sum(e["type"] == "final" for e in events), 1, result.stdout+result.stderr)
        return result, events, events[-1]["result"]

    def test_exec_stdin_resume_and_pure_json_exit_without_terminal(self):
        first, events, result = self.cli("exec", "-", stdin="你好\nAnalyze this sentence")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(result["text"], "CLI_RESULT_你好")
        self.assertTrue(result["persisted"])
        sid = result["session_id"]
        second, _, result = self.cli("exec", "Continue", "--session", sid)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(result["session_id"], sid)
        self.assertIn("Analyze this sentence", json.dumps(self.requests[-1], ensure_ascii=False))

    def test_context_cli_native_checkpoint_roundtrip_through_actual_sdk(self):
        self.mode = "responses"
        directory = self.root / "config"; directory.mkdir()
        (directory/"config.json").write_text(json.dumps({"active_model_connection":"owned", "model_connections":[{
            "id":"owned", "name":"Owned fixture", "provider":"openai", "model":"fixture", "models":["fixture"],
            "base_url":self.env["OPENAI_BASE_URL"], "api_key":"fixture", "model_context":{"fixture":{"window":32768,"backend":"openai-responses"}}}]}))
        process, _, result = self.cli("exec", "Original evidence: " + "source "*2500)
        self.assertEqual(process.returncode, 0, process.stderr + str(result))
        sid = result["session_id"]
        process, _, report = self.cli("context", "status", "--session", sid)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(report["context"]["window"], 32768)
        process, _, report = self.cli("context", "compact", "--session", sid, "--focus", "Keep evidence")
        self.assertEqual(process.returncode, 0, process.stderr + str(report))
        self.assertLess(report["context"]["last"]["after"], report["context"]["last"]["before"])
        process, _, result = self.cli("exec", "Continue", "--session", sid)
        self.assertEqual(process.returncode, 0, process.stderr + str(result))
        self.assertEqual(result["text"], "NATIVE_CONTEXT_OK")
        self.assertEqual(self.requests[-1]["input"][0], {"type":"compaction","id":"opaque-owned","encrypted_content":"KEEP_CANONICAL"})
        self.assertEqual(self.requests[-1]["input"][-1]["content"], "Continue")
        self.assertFalse(any(row["path"].endswith("/chat/completions") for row in self.wire))

    def test_followup_watch_waits_without_requests_and_serves_once_after_restart(self):
        process, _, registered = self.cli("schedule", "watch", "Inspect experiment", "--marker-file", "experiment.done", "--request-id", "experiment")
        self.assertEqual(process.returncode, 0, process.stderr)
        sid, identity = registered["session_id"], registered["schedule"]["id"]
        _, _, duplicate = self.cli("schedule", "watch", "Inspect experiment", "--marker-file", "experiment.done", "--request-id", "experiment", "--session", sid)
        self.assertEqual(duplicate["schedule"]["id"], identity)
        process, events, _ = self.cli("schedule", "serve", "--once")
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertFalse(self.requests)
        self.assertFalse(any(e["type"] == "scheduled_delivery" for e in events))
        (self.root / "experiment.done").write_text("ready")
        path = self.root / "data/schedules.json"
        rows = json.loads(path.read_text()); rows["items"][identity]["due_at"] = 0
        path.write_text(json.dumps(rows))
        process, events, _ = self.cli("schedule", "serve", "--once")
        self.assertEqual(process.returncode, 0, process.stderr)
        receipt = next(e["receipt"] for e in events if e["type"] == "scheduled_delivery")
        self.assertEqual(receipt["input_status"], "processed")
        self.assertEqual(receipt["acceptance"], "execution_only")
        self.assertEqual(len(self.requests), 1)
        self.assertIn("marker_observed_not_task_verified", json.dumps(self.requests[0]))
        process, _, _ = self.cli("schedule", "serve", "--once")
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(len(self.requests), 1)

    def test_native_search_discovery_from_formal_cli_uses_saved_connection(self):
        self.mode = 'native-search'
        config = self.root / 'config'; config.mkdir()
        (config / 'config.json').write_text(json.dumps({'active_model_connection': 'parent', 'model_connections': [
            {'id': 'parent', 'provider': 'openai', 'model': 'fixture', 'models': ['fixture'], 'base_url': self.env['OPENAI_BASE_URL'],
                'api_key': 'fixture', 'native_search': {'backend': 'deepseek', 'model': '', 'baseUrl': ''}}]}), encoding='utf-8')
        process, events, result = self.cli('exec', 'Search the web', '--permission', 'full')
        self.assertEqual(process.returncode, 0, process.stderr + json.dumps(events))
        self.assertTrue(result['persisted'])
        self.assertEqual(len(self.requests), 3)
        first = self.requests[0]
        compact = lambda value: json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        system = '\n'.join(row['content'] for row in first['messages'] if row['role'] == 'system')
        size = len(system) + len(compact(first['tools'])) + len(compact([row for row in first['messages'] if row['role'] != 'system']))
        self.assertLess(size, 6500, 'Empty-profile first requests must stay near the Pi baseline')
        self.assertNotIn('web_search_tool', [row['function']['name'] for row in self.requests[0]['tools']])
        search = next(row['function'] for row in self.requests[1]['tools'] if row['function']['name'] == 'web_search_tool')
        self.assertEqual(search['parameters']['properties']['query']['maxLength'], 128)
        last = json.dumps(self.requests[-1]['messages'])
        self.assertIn('https://evidence.invalid/paper', last)
        self.assertIn('LOCAL_SEARCH_EVIDENCE', last)
        raw_search = [row for row in self.wire if row['path'] == '/v1/tools/search']
        self.assertEqual(len(raw_search), 1)
        self.assertEqual(raw_search[0]['headers']['Authorization'], 'Bearer fixture')

    def test_search_config_formal_entry_does_not_start_engine_or_request_model(self):
        config = self.root / 'config'; config.mkdir()
        (config / 'config.json').write_text(json.dumps({'active_model_connection': 'parent', 'model_connections': [
            {'id': 'parent', 'provider': 'openai', 'model': 'fixture', 'models': ['fixture'], 'base_url': self.env['OPENAI_BASE_URL'], 'api_key': 'fixture-secret'}]}), encoding='utf-8')
        process = subprocess.run([sys.executable, '-m', 'xuloom', 'search-config', '--backend', 'deepseek', '--json'],
            cwd=self.root, capture_output=True, text=True, encoding='utf-8', env=self.env, timeout=35)
        self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
        events = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(events[-1]['type'], 'final')
        self.assertEqual(events[-1]['result']['search']['backend'], 'deepseek')
        self.assertNotIn('fixture-secret', process.stdout)
        self.assertEqual(self.wire, [])

    def test_native_named_connection_role_from_formal_cli(self):
        self.mode = "heterogeneous"
        config = self.root / "config"; config.mkdir()
        (config / "config.json").write_text(json.dumps({"active_model_connection": "parent", "model_connections": [
            {"id": "parent", "provider": "openai", "model": "fixture", "models": ["fixture"], "base_url": self.env["OPENAI_BASE_URL"], "api_key": "fixture"},
            {"id": "other", "provider": "openai", "model": "other-model", "models": ["other-model"], "base_url": self.env["OPENAI_BASE_URL"].replace("/v1", "/worker/v1"), "api_key": "worker-key"}]}), encoding="utf-8")
        roles = self.root / ".xuloom/agents"; roles.mkdir(parents=True)
        (roles / "other-account.md").write_text("---\nname: other-account\ndescription: independent check\nconnection: other\n---\nCheck evidence independently.\n", encoding="utf-8")
        process, events, final = self.cli("exec", "delegate once", "--permission", "full", "--collaboration", "auto")
        self.assertEqual(process.returncode, 0, process.stderr + json.dumps(events))
        worker = [r for r in self.wire if r["path"].startswith("/worker/")]
        self.assertEqual(len(worker), 1)
        self.assertEqual(worker[0]["headers"]["authorization"], "Bearer worker-key")
        self.assertEqual(worker[0]["body"]["model"], "other-model")
        for request in self.wire:
            if not request["path"].startswith("/worker/"):
                self.assertEqual(request["headers"]["authorization"], "Bearer fixture")
        self.assertTrue(final["persisted"])

    def test_corrupt_session_returns_conflict_without_model_retry_or_overwrite(self):
        from unittest.mock import patch
        from xuloom.core.session import SessionManager
        initial, _, saved = self.cli("exec", "Seed conversation")
        self.assertEqual(initial.returncode, 0, initial.stderr)
        sid = saved["session_id"]
        with patch.dict(os.environ, self.env), SessionManager(cwd=self.root) as manager:
            checkpoint = Path(manager.session_dir) / (sid + ".json")
        raw = json.loads(checkpoint.read_bytes())
        self.mode = "write"
        count = len(self.wire)
        for revision in (None, True, -1, "7"):
            with self.subTest(revision=revision):
                checkpoint.write_text(json.dumps({**raw, "revision": revision}), encoding="utf-8")
                before = checkpoint.read_bytes()
                process, _, result = self.cli("exec", "Continue and write", "--session", sid, "--permission", "full")
                self.assertEqual(process.returncode, 4, process.stderr)
                self.assertEqual(result["reason"], "session_conflict")
                self.assertEqual(len(self.wire), count)
                self.assertEqual(checkpoint.read_bytes(), before)
                self.assertFalse((self.root / "effect.txt").exists())

    def test_empty_desktop_draft_can_start_in_cli_without_closing_its_current_client(self):
        from unittest.mock import patch
        from xuloom.core.session import SessionManager
        with patch.dict(os.environ, self.env), SessionManager(cwd=self.root) as manager:
            self.assertTrue(manager.save_session_state("empty-desktop-draft", [], metadata={}))
        process, _, result = self.cli("exec", "Start this empty draft", "--session", "empty-desktop-draft")
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(result["text"], "CLI_RESULT_你好")
        self.assertEqual(result["session_id"], "empty-desktop-draft")
        self.assertEqual(len(self.requests), 1)

    def test_collaboration_flag_and_resume_preserve_mode_without_changing_permissions(self):
        first, _, result = self.cli("exec", "Hello", "--collaboration", "ultra")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(len(self.requests), 1)
        self.assertTrue({'spawn_agent','web_search_tool','web_fetch_tool'} <= {t['function']['name'] for t in self.requests[0]['tools']})
        self.assertNotIn('tool_choice',self.requests[0])
        self.assertEqual(result["text"], "CLI_RESULT_你好")
        self.assertIn("# Collaboration: ultra", json.dumps(self.requests[-1]))
        sid = result["session_id"]
        second, _, result = self.cli("exec", "Again", "--session", sid)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("# Collaboration: ultra", json.dumps(self.requests[-1]))
        third, _, result = self.cli("exec", "Again", "--session", sid, "--collaboration", "solo")
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertIn("# Collaboration: solo", json.dumps(self.requests[-1]))

    def test_required_approval_is_explicit_and_no_write_is_performed(self):
        self.mode = "write"
        process, events, result = self.cli("exec", "Write the file")
        self.assertEqual(process.returncode, 3, process.stderr)
        self.assertEqual(result["reason"], "approval_required")
        self.assertTrue(any(e["type"] == "approval_required" for e in events))
        self.assertFalse((self.root/"effect.txt").exists())
        allowed, _, result = self.cli("exec", "Write the file", "--permission", "full")
        self.assertEqual(allowed.returncode, 0, allowed.stderr)
        self.assertEqual((self.root/"effect.txt").read_text(), "ONCE")

    def test_goal_budget_and_status_are_noninteractive_and_durable(self):
        process, _, result = self.cli("goal", "start", "Do work", "--criterion", "Save evidence", "--rounds", "1")
        self.assertEqual(process.returncode, 8, process.stderr)
        self.assertEqual(result["goal"]["status"], "budget_exhausted")
        count = len(self.requests)
        status, _, saved = self.cli("goal", "status", "--session", result["session_id"])
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(saved["goal"]["rounds"], 1)
        self.assertEqual(len(self.requests), count)

    def test_model_question_never_reads_stdin_or_assumes_a_user_answer(self):
        self.mode = "question"
        process, events, result = self.cli("exec", "Ask what is missing")
        self.assertEqual(process.returncode, 3, process.stderr)
        self.assertEqual(result["reason"], "input_required")
        self.assertTrue(any(e["type"] == "input_required" for e in events))
        self.assertEqual(len(self.requests), 1, "No model retry after missing user input")

    def test_due_message_delivers_to_same_session_and_second_worker_does_not_repeat(self):
        _, _, initial = self.cli("exec", "Seed conversation")
        sid = initial["session_id"]
        added, _, result = self.cli("schedule", "add", "Check experiment", "--session", sid, "--delay", "0")
        self.assertEqual(added.returncode, 0, added.stderr)
        count = len(self.requests)
        delivered, events, _ = self.cli("schedule", "serve", "--once")
        self.assertEqual(delivered.returncode, 0, delivered.stderr)
        self.assertTrue(any(e["type"] == "scheduled_delivery" and e["receipt"]["status"] == "completed" for e in events))
        self.assertEqual(len(self.requests), count+1)
        self.cli("schedule", "serve", "--once")
        self.assertEqual(len(self.requests), count+1)
        self.assertIn("Check experiment", json.dumps(self.requests[-1]))

    def test_another_process_pauses_the_goal_without_taking_its_writer_lease(self):
        self.mode = "hold"
        process = subprocess.Popen([sys.executable, "-m", "xuloom", "goal", "start", "Wait for result", "--criterion", "Inspect result", "--json", "--cwd", str(self.root)],
            cwd=self.root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=self.env)
        lines = queue.Queue()
        reader = threading.Thread(target=lambda: [lines.put(line) for line in process.stdout], daemon=True); reader.start()
        try:
            started = json.loads(lines.get(timeout=20))
            self.assertEqual(started["type"], "session_started")
            self.assertTrue(self.request_started.wait(15))
            pause, _, receipt = self.cli("goal", "pause", "--session", started["session_id"])
            self.assertEqual(pause.returncode, 0, pause.stderr)
            self.assertEqual(receipt["control_requested"], "pause")
            self.assertEqual(process.wait(15), 6, process.stderr.read())
            reader.join(3)
            events = []
            while not lines.empty(): events.append(json.loads(lines.get_nowait()))
            self.assertEqual(events[-1]["result"]["reason"], "paused")
            self.assertEqual(len(self.requests), 1)
        finally:
            self.finish_request.set()
            if process.poll() is None: process.kill(); process.wait(5)  # this fixture's subprocess only
            process.stdout.close(); process.stderr.close()
