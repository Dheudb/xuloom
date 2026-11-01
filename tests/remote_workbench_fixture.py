"""Isolated SSH + model services for hidden Electron tests; not a product server."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
from pathlib import Path
import sys
import threading
import subprocess
import wave

from ssh_fixture import SSHFixture, paramiko


def main():
    if paramiko is None: raise SystemExit("Install the dev extra for encrypted SSH desktop tests")
    root = Path(sys.argv[1]); remote = root / "remote files"; remote.mkdir()
    subprocess.run(["git", "init", "-b", "remote-main", str(remote)], check=True, capture_output=True)
    (remote / "report.md").write_text("# Remote document\nREMOTE_FILE_SOURCE", encoding="utf-8")
    with wave.open(str(remote / "remote-audio.wav"), "wb") as output:
        output.setparams((1, 2, 8000, 0, "NONE", "not compressed")); output.writeframes(b"\0\0" * 16000)
    configuration = root / "remote-config"; configuration.mkdir()
    records = root / "wire.jsonl"; lock = threading.Lock()
    hold = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                with records.open("a", encoding="utf-8") as output: output.write(json.dumps({"path": self.path, "key": self.headers.get("Authorization"), "body": body})+"\n")
            if not body.get("stream"):
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
                self.wfile.write(json.dumps({"choices": [{"message": {"role": "assistant", "content": "memory"}}]}).encode()); return
            last = next((row for row in reversed(body["messages"]) if row["role"] == "user"), {})
            text = json.dumps(last.get("content", ""))
            has_tool = body["messages"][-1]["role"] == "tool"
            delta, finish = {"content": "REMOTE_REPLY_"+body["model"]}, "stop"
            if "verify remote attachments" in text:
                blocks = last.get("content", [])
                pixels = isinstance(blocks, list) and any(block.get("type") == "image_url" and block.get("image_url", {}).get("url", "").startswith("data:image/jpeg;base64,") for block in blocks)
                refs = []
                for block in blocks if isinstance(blocks, list) else []:
                    if block.get("type") == "text":
                        for line in block.get("text", "").splitlines():
                            try:
                                item = json.loads(line)
                                if isinstance(item, dict) and item.get("name") == "research notes.txt": refs.append(item)
                            except ValueError: pass
                if not pixels or not refs: delta = {"content": "ATTACHMENTS_MISSING"}
                elif not has_tool:
                    delta, finish = {"tool_calls": [{"index": 0, "id": "read-attachment", "type": "function", "function": {"name": "file_read_tool", "arguments": json.dumps({"file_path": refs[0]["path"]})}}]}, "tool_calls"
                else:
                    delta = {"content": "REMOTE_ATTACHMENTS_VERIFIED" if "REMOTE_UPLOAD_DOCUMENT" in str(body["messages"][-1]) else "ATTACHMENT_READ_FAILED"}
            if "delegate remote fixture" in text and self.path.startswith("/local-models/"):
                tools = [row for row in body["messages"] if row["role"] == "tool"]
                if not any(row.get("tool_call_id") == "spawn-remote-ui" for row in tools):
                    name, identity, arguments = "spawn_agent", "spawn-remote-ui", {"prompt": "Greet once from this execution host", "subagent_type": "remote-auditor"}
                elif tools[-1].get("tool_call_id") == "spawn-remote-ui":
                    child = re.search(r"subagent-[a-f0-9-]+", str(tools[-1]["content"])).group()
                    name, identity, arguments = "agent_tool", "wait-remote-ui", {"action": "wait", "agent_ids": [child], "timeout_ms": 30000, "response_format": "json"}
                else:
                    name = None
                    delta = {"content": "REMOTE_WORKER_VERIFIED" if "REMOTE_REPLY_remote-b" in str(tools) else "REMOTE_WORKER_NOT_READY"}
                if name:
                    delta, finish = {"tool_calls": [{"index": 0, "id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}, "tool_calls"
            if "write report" in text and not has_tool:
                delta, finish = {"tool_calls": [{"index": 0, "id": "write-report", "type": "function", "function": {"name": "file_write_tool", "arguments": json.dumps({"file_path": str(remote/"generated.md"), "content": "# Created remotely\nREMOTE_WRITTEN_ARTIFACT"})}}]}, "tool_calls"
            if "choose" in text and not has_tool:
                delta, finish = {"tool_calls": [{"index": 0, "id": "choose", "type": "function", "function": {"name": "ask_user_question_tool", "arguments": json.dumps({"questions": [{"question": "Remote choice?", "options": [{"label": "Choice A"}, {"label": "Choice B"}]}]})}}]}, "tool_calls"
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            def frame(delta, finish=None): return ("data: "+json.dumps({"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})+"\n\n").encode()
            try:
                self.wfile.write(frame(delta)); self.wfile.flush()
                if "hold" in text: hold.wait(60)
                self.wfile.write(frame({}, finish)+b"data: [DONE]\n\n")
            except OSError: pass
    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True); thread.start()
    model = lambda identity, key, models: {"id": identity, "name": identity, "provider": "openai", "model": models[0], "models": models, "api_key": key, "base_url": f"http://127.0.0.1:{http.server_port}/{identity}/v1"}
    (configuration/"config.json").write_text(json.dumps({"active_model_connection": "remote-models", "model_connections": [model("remote-models", "remote-key", ["remote-a", "remote-b"])]}), encoding="utf-8")
    environment = {key: value for key,value in os.environ.items() if not key.startswith(("XULOOM_", "OPENAI_", "ANTHROPIC_"))}
    environment.update(XULOOM_CONFIG_DIR=str(configuration), XULOOM_DATA_DIR=str(root/"remote-data"), XULOOM_SKILLS_PATHS="", HOME=str(root/"remote-home"), USERPROFILE=str(root/"remote-home"))
    ssh = SSHFixture(root, environment)
    profile = {"id": "lab", "name": "SSH Lab", "host": "loopback-fixture", "cwd": str(remote), "python": os.environ.get("XULOOM_TEST_REMOTE_PYTHON", sys.executable), "platform": "windows" if os.name == "nt" else "posix", "ssh_config": str(ssh.config)}
    local = root/"local-config"; local.mkdir()
    (local/"config.json").write_text(json.dumps({"active_model_connection": "local-models", "ssh_connections": [profile], "model_connections": [model("local-models", "local-key", ["local-a"])]}), encoding="utf-8")
    print(json.dumps({"profile": profile, "remote": str(remote), "config": str(local)}), flush=True)
    try:
        for line in sys.stdin:
            action = json.loads(line).get("action")
            if action == "disconnect": ssh.disconnect()
            if action == "release": hold.set()
            if action == "stop": break
    finally:
        hold.set(); ssh.close(); http.shutdown(); http.server_close(); thread.join(5)


if __name__ == "__main__": main()
