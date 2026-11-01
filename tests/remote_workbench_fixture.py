"""Isolated SSH + model services for hidden Electron tests; not a product server."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading
import subprocess

from ssh_fixture import SSHFixture, paramiko


def main():
    if paramiko is None: raise SystemExit("Install the dev extra for encrypted SSH desktop tests")
    root = Path(sys.argv[1]); remote = root / "remote files"; remote.mkdir()
    subprocess.run(["git", "init", "-b", "remote-main", str(remote)], check=True, capture_output=True)
    (remote / "report.md").write_text("# Remote document\nREMOTE_FILE_SOURCE", encoding="utf-8")
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
