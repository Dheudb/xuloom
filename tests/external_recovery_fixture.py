"""Actual stopped/crashed Pi workers for the hidden desktop recovery check."""
import asyncio
import json
import os
from pathlib import Path
import sys
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
if not os.environ.get("XULOOM_TEST_PI_PROGRAM"): raise SystemExit("An installed test Pi program is required")
PROTOCOL = sys.stdout
sys.stdout = sys.stderr

import test_external_agents as external
from test_pi_agents import InstalledPiTests
from xuloom.core.config import save_config


async def main():
    fixture = InstalledPiTests()
    await fixture.asyncSetUp()
    try:
        fixture.role(); fixture.mode = "hold"; engine = fixture.engine()
        engine.require_writer()
        engine.messages.append(external.create_user_message("Inspect external worker recovery"))
        assert engine.persist_session_state()
        confirmed = await fixture.spawn(engine, description="Confirmed external stop", run_in_background=True)
        await fixture.wait_for_requests(1)
        stopped = await engine.agents("cancel", agent_id=confirmed["agent_id"])
        assert stopped["data"]["external_recovery"]["resumable"]
        external_identity = engine.session_manager.load_subagent_transcript(confirmed["agent_id"])["metadata"]["external_agent"]["session_id"]
        fixture.release.set(); fixture.release = threading.Event()
        processes = []
        create = asyncio.create_subprocess_exec
        async def capture(*args, **kwargs):
            process = await create(*args, **kwargs)
            if "--mode" in args: processes.append(process)
            return process
        with patch("asyncio.create_subprocess_exec", capture):
            uncertain = await fixture.spawn(engine, description="Unconfirmed external stop", run_in_background=True)
            await fixture.wait_for_requests(2)
            assert len(processes) == 1
            processes[0].kill(); await processes[0].wait()
            result = await engine.agents("wait", agent_ids=[uncertain["agent_id"]], timeout_ms=10000)
            assert result["data"]["agents"][0]["status"] == "crashed"
        fixture.release.set()
        assert len(fixture.records) == 2
        identity = engine.session_id
        await engine.aclose()
        config = fixture.root / "config"; config.mkdir(exist_ok=True)
        save_config({"model_connections": [{"id": "local-fixture", "name": "Local protocol fixture", "provider": "anthropic",
            "model": external.MODEL, "models": [external.MODEL], "api_key": "fixture-only", "base_url": fixture.base,
            "network_mode": "direct"}], "active_model_connection": "local-fixture"})
        (config / "config.json").write_bytes((fixture.root / "config.json").read_bytes())
        print(json.dumps({"home": str(fixture.root), "project": str(fixture.root), "data": str(fixture.root / "data"),
            "config": str(config), "sessionId": identity, "confirmedId": confirmed["agent_id"], "uncertainId": uncertain["agent_id"]}), file=PROTOCOL, flush=True)
        await asyncio.to_thread(sys.stdin.readline)
        assert len(fixture.records) == 2, "Desktop inspection must not start a model request or replay a worker"
        parent_requests = []
        def parent_reply(handler, body):
            if handler.path.endswith("/messages/count_tokens"):
                raw = json.dumps({"input_tokens": 500}).encode()
                handler.send_response(200); handler.send_header("Content-Type", "application/json"); handler.send_header("Content-Length", str(len(raw)))
                handler.end_headers(); handler.wfile.write(raw)
                return True
            if body.get("stream") is not True:
                raw = json.dumps({"id": "fixture_memory", "type": "message", "role": "assistant", "model": body["model"],
                    "content": [{"type": "text", "text": "Fixture summary: retain the confirmed worker identity and pending results."}],
                    "stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 5}}).encode()
                handler.send_response(200); handler.send_header("Content-Type", "application/json"); handler.send_header("Content-Length", str(len(raw)))
                handler.end_headers(); handler.wfile.write(raw)
                return True
            if not any(tool.get("name") == "agent_tool" for tool in body.get("tools", [])): return False
            parent_requests.append(body)
            final = len(parent_requests) > 1
            block = {"type": "text", "text": ""} if final else {"type": "tool_use", "id": "toolu_cli_followup", "name": "agent_tool", "input": {}}
            arguments = {"action": "followup", "agent_id": confirmed["agent_id"], "prompt": "Continue from the confirmed stop"}
            delta = {"type": "text_delta", "text": "CLI_CONFIRMED_RESUMED"} if final else {"type": "input_json_delta", "partial_json": json.dumps(arguments)}
            events = [{"type": "message_start", "message": {"id": "cli_fixture", "type": "message", "role": "assistant", "model": body["model"], "content": [],
                "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 0}}},
                {"type": "content_block_start", "index": 0, "content_block": block}, {"type": "content_block_delta", "index": 0, "delta": delta},
                {"type": "content_block_stop", "index": 0}, {"type": "message_delta", "delta": {"stop_reason": "end_turn" if final else "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 5}},
                {"type": "message_stop"}]
            raw = "".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n" for event in events).encode()
            handler.send_response(200); handler.send_header("Content-Type", "text/event-stream"); handler.send_header("Content-Length", str(len(raw)))
            handler.end_headers(); handler.wfile.write(raw)
            return True
        fixture.parent_handler = parent_reply; fixture.mode = "normal"
        program = os.environ.get("XULOOM_TEST_LOCAL_PYTHON")
        command = [program, "-I", "-B"] if program else [sys.executable]
        command += ["-m", "xuloom", "exec", "--cwd", str(fixture.root), "--session", identity, "--json", "--permission", "full", "--collaboration", "ultra", "Resume the confirmed external worker"]
        env = {**os.environ, "HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1", "ALL_PROXY": "http://127.0.0.1:1",
            "NO_PROXY": "127.0.0.1,localhost,::1", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
        for key in list(env):
            if key.startswith(("OPENAI_", "ANTHROPIC_")): env.pop(key)
        env.pop("XULOOM_MODEL_PROVIDER", None)
        env.update(XULOOM_CONFIG_DIR=str(config), XULOOM_NETWORK_MODE="direct")
        process = await asyncio.create_subprocess_exec(*command, cwd=ROOT, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **({"creationflags": 0x08000000} if os.name == "nt" else {}))
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
        finally:
            if process.returncode is None: process.kill(); await process.wait()
        rows = [json.loads(line) for line in stdout.split(b"\n") if line.strip()]
        assert process.returncode == 0 and rows[-1]["type"] == "final" and rows[-1]["result"]["reason"] == "completed", json.dumps({
            "exit": process.returncode, "final": rows[-1], "parents": len(parent_requests), "requests": len(fixture.records), "stderr": stderr.decode(errors="replace")[-3000:]})
        assert "CLI_CONFIRMED_RESUMED" in json.dumps(rows)
        saved = engine.session_manager.load_subagent_transcript(confirmed["agent_id"])
        state = saved["metadata"]["external_agent"]
        assert saved["metadata"]["external_agent"]["phase"] == "idle" and saved["metadata"]["last_run_result"]["status"] == "completed", json.dumps({
            "phase": state["phase"], "receipt": saved["metadata"]["last_run_result"], "parents": len(parent_requests), "requests": len(fixture.records), "saved_count": state["message_count"]})
        assert saved["metadata"]["external_agent"]["session_id"] == external_identity
        assert len(saved["metadata"]["tool_activity"]["recent"]) == 1
        inference = [row for row in fixture.records if row["body"].get("stream") is True]
        assert len(parent_requests) >= 2 and len(inference) == 4 + len(parent_requests), (len(parent_requests), len(inference))
    finally:
        await fixture.asyncTearDown()


if __name__ == "__main__": asyncio.run(main())
