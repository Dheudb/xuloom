"""Own localhost service for App and formal CLI resuming an installed worker."""
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
PROTOCOL = sys.stdout
sys.stdout = sys.stderr

import test_external_agents as fixtures
from external_discovery_api import CatalogModel
from xuloom.core.config import save_config


async def main():
    fixture = fixtures.ExternalFixture(); await fixture.asyncSetUp()
    try:
        backend = os.environ.get('XULOOM_TEST_DISCOVERY_BACKEND', 'pi')
        if backend not in {'pi', 'dsh'}: raise ValueError('Unsupported discovery fixture backend')
        program = os.environ['XULOOM_TEST_' + backend.upper() + '_PROGRAM']
        folder = fixture.root / ".xuloom/agents"; folder.mkdir(parents=True)
        (folder / "discoverer.md").write_text("---\nname: discoverer\ndescription: inspect background tasks\nbackend: " + backend + "\nbackend_program: "
            + program.replace("\\", "/") + "\ntools: ['*']\n---\nComplete the assigned task.\n", encoding="utf8")
        model = CatalogModel(); phase, worker_id = "bootstrap", None
        parent_counts = {"bootstrap": 0, "app": 0, "cli": 0}
        def respond(handler, body):
            if handler.path.endswith("/messages/count_tokens") or body.get("stream") is not True: return model(handler, body)
            if not any(tool.get("name") == "agent_tool" for tool in body.get("tools", [])): return model(handler, body)
            parent_counts[phase] += 1
            values = {"action": "spawn", "subagent_type": "discoverer", "prompt": "Discover and activate task_list_tool, then list background tasks"} if phase == "bootstrap" else {
                "action": "followup", "agent_id": worker_id, "prompt": "List background tasks again using the already activated tool"}
            return model.reply(handler, body, name="agent_tool" if parent_counts[phase] == 1 else None,
                values=values, text=phase.upper() + "_CATALOG_COMPLETE", identity="parent_" + phase + "_" + str(parent_counts[phase]))
        fixture.parent_handler = respond
        engine = fixture.engine()
        result = await engine.run(user_input="Inspect discovered worker", sys_print_callback=lambda _: None)
        assert result.ok, result
        row = engine.subagent_registry[0]; assert row["status"] == "completed", row
        worker_id, identity = row["agent_id"], engine.session_id
        before = engine.session_manager.load_subagent_transcript(worker_id)
        external_id = before["metadata"]["external_agent"]["session_id"]
        assert len(before["metadata"]["tool_activity"]["recent"]) == 2
        await engine.aclose()
        config = fixture.root / "config"; config.mkdir(exist_ok=True)
        save_config({"model_connections": [{"id": "local-fixture", "name": "Local protocol fixture", "provider": "anthropic",
            "model": fixtures.MODEL, "models": [fixtures.MODEL], "api_key": "fixture-only", "base_url": fixture.base, "network_mode": "direct"}],
            "active_model_connection": "local-fixture"})
        (config / "config.json").write_bytes((fixture.root / "config.json").read_bytes())
        phase = "app"
        print(json.dumps({"home": str(fixture.root), "project": str(fixture.root), "data": str(fixture.root / "data"), "config": str(config),
            "sessionId": identity, "workerId": worker_id, "baseUrl": fixture.base}), file=PROTOCOL, flush=True)
        await asyncio.to_thread(sys.stdin.readline)
        after_app = engine.session_manager.load_subagent_transcript(worker_id)
        assert after_app["metadata"]["last_run_result"]["status"] == "completed"
        assert len(after_app["metadata"]["tool_activity"]["recent"]) == 3 and len(model.catalogs) == 5, {
            "receipts": len(after_app["metadata"]["tool_activity"]["recent"]), "requests": len(model.catalogs), "parentCounts": parent_counts}
        phase = "cli"
        program = os.environ.get("XULOOM_TEST_LOCAL_PYTHON")
        command = [program, "-I", "-B"] if program else [sys.executable]
        command += ["-m", "xuloom", "exec", "--cwd", str(fixture.root), "--session", identity, "--json", "--permission", "full", "Resume via CLI"]
        env = {**os.environ, "HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1", "ALL_PROXY": "http://127.0.0.1:1",
            "NO_PROXY": "127.0.0.1,localhost,::1", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
        for key in list(env):
            if key.startswith(("OPENAI_", "ANTHROPIC_")): env.pop(key)
        env.pop("XULOOM_MODEL_PROVIDER", None)
        env.update(XULOOM_CONFIG_DIR=str(config), XULOOM_NETWORK_MODE="direct")
        process = await asyncio.create_subprocess_exec(*command, cwd=ROOT, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **({"creationflags": 0x08000000} if os.name == "nt" else {}))
        try: stdout, stderr = await asyncio.wait_for(process.communicate(), 40)
        finally:
            if process.returncode is None: process.kill(); await process.wait()
        rows = [json.loads(line) for line in stdout.split(b"\n") if line.strip()]
        assert process.returncode == 0 and rows[-1]["type"] == "final" and rows[-1]["result"]["reason"] == "completed", stderr.decode(errors="replace")[-1000:]
        assert "CLI_CATALOG_COMPLETE" in json.dumps(rows)
        saved = engine.session_manager.load_subagent_transcript(worker_id)
        assert saved["metadata"]["external_agent"]["session_id"] == external_id
        assert saved["metadata"]["activated_tools"] == ["task_list_tool"]
        assert saved["metadata"]["last_run_result"]["status"] == "completed"
        receipts = saved["metadata"]["tool_activity"]["recent"]
        assert len(receipts) == 4 and all(record["status"] == "ok" for record in receipts)
        assert sum(record["tool_name"] == "tool_search_tool" for record in receipts) == 1
        assert len(model.catalogs) == 7 and all("mcp__xuloom__task_list_tool" in names for names in model.catalogs[1:])
        assert all("mcp__xuloom__browser_tool" not in names for names in model.catalogs)
    finally: await fixture.asyncTearDown()


if __name__ == "__main__": asyncio.run(main())
