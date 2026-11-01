"""Real installed CLIs must execute a discovered tool, not merely print success."""
import os
import json
import asyncio
from pathlib import Path
import sys
import unittest

if __package__:
    from . import test_external_agents as fixtures
    from .external_discovery_api import CatalogModel
else:
    import test_external_agents as fixtures
    from external_discovery_api import CatalogModel


class DiscoveryContract(fixtures.ExternalFixture):
    async def scenario(self, backend, program, provider="anthropic"):
        folder = self.root / ".xuloom/agents"; folder.mkdir(parents=True)
        (folder / "discoverer.md").write_text("---\nname: discoverer\ndescription: inspect background tasks\nbackend: " + backend
            + "\nbackend_program: " + str(program).replace("\\", "/") + "\ntools: ['*']\n---\nComplete the assigned task.\n", encoding="utf8")
        model = CatalogModel(provider); catalogs = model.catalogs
        self.parent_handler = model
        engine = self.engine(provider)
        response = await engine.agents("spawn", prompt="Discover and activate task_list_tool, then list background tasks", subagent_type="discoverer")
        self.assertTrue(response["ok"], response)
        row = response["data"]["agents"][0]
        self.assertEqual(row["status"], "completed", row)
        saved = engine.session_manager.load_subagent_transcript(row["agent_id"])
        self.assertEqual(saved["metadata"]["activated_tools"], ["task_list_tool"])
        receipts = saved["metadata"]["tool_activity"]["recent"]
        self.assertEqual({record["tool_name"] for record in receipts}, {"tool_search_tool", "task_list_tool"})
        self.assertTrue(all(record["status"] == "ok" for record in receipts), receipts)
        external_id = saved["metadata"]["external_agent"]["session_id"]
        parent_id = engine.session_id
        await engine.aclose()
        restored = self.engine(provider); self.assertTrue(restored.load_session(parent_id))
        followup = await restored.agents("followup", agent_id=row["agent_id"], prompt="List background tasks again using the already activated tool")
        self.assertEqual(followup["data"]["status"], "completed", followup)
        after = restored.session_manager.load_subagent_transcript(row["agent_id"])
        self.assertEqual(after["metadata"]["external_agent"]["session_id"], external_id)
        self.assertEqual(after["metadata"]["activated_tools"], ["task_list_tool"])
        receipts = after["metadata"]["tool_activity"]["recent"]
        self.assertEqual(len(receipts), 3)
        self.assertTrue(all(record["status"] == "ok" for record in receipts), receipts)
        self.assertEqual(sum(record["tool_name"] == "tool_search_tool" for record in receipts), 1)
        self.assertEqual(len(catalogs), 5)
        self.assertNotIn("mcp__xuloom__task_list_tool", catalogs[0])
        self.assertTrue(all("mcp__xuloom__task_list_tool" in names for names in catalogs[1:]))
        self.assertTrue(all("mcp__xuloom__browser_tool" not in names for names in catalogs), "Unselected tools reached the model")
        first = next(record["body"] for record in self.records if record["body"].get("stream") is True)
        system = first.get("system", [message for message in first["messages"] if message.get("role") in {"system", "developer"}])
        self.assertNotIn("mcp__xuloom__browser_tool", json.dumps(system), "The CLI leaked unselected definitions through its system prompt")


@unittest.skipUnless(fixtures.MCP and os.environ.get("XULOOM_TEST_PI_PROGRAM"), "set XULOOM_TEST_PI_PROGRAM for installed Pi")
class InstalledPiDiscovery(DiscoveryContract):
    async def test_help_probe_does_not_wait_for_or_consume_the_controller_input(self):
        from xuloom.agents.external import program
        code = ("import asyncio,sys;sys.path.insert(0,sys.argv[1]);"
            "from xuloom.agents.external.program import resolve,verify;"
            "asyncio.run(verify(resolve(sys.argv[2],backend='pi')));"
            "print('READY',flush=True);print(sys.stdin.readline().strip(),flush=True)")
        process = await asyncio.create_subprocess_exec(sys.executable, "-I", "-B", "-c", code,
            str(Path(program.__file__).resolve().parents[3]), os.environ["XULOOM_TEST_PI_PROGRAM"], cwd=self.root,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **({"creationflags": 0x08000000} if os.name == "nt" else {}))
        try:
            self.assertEqual((await asyncio.wait_for(process.stdout.readline(), 15)).strip(), b"READY")
            process.stdin.write(b"CONTROL_RECORD\n"); await process.stdin.drain()
            self.assertEqual((await asyncio.wait_for(process.stdout.readline(), 5)).strip(), b"CONTROL_RECORD")
            self.assertEqual(await asyncio.wait_for(process.wait(), 5), 0)
        finally:
            if process.returncode is None: process.kill(); await process.wait()

    async def test_discovery_and_restored_followup_anthropic(self):
        await self.scenario("pi", os.environ["XULOOM_TEST_PI_PROGRAM"])

    async def test_discovery_and_restored_followup_openai(self):
        await self.scenario("pi", os.environ["XULOOM_TEST_PI_PROGRAM"], "openai")


@unittest.skipUnless(fixtures.MCP and os.environ.get("XULOOM_TEST_CLAUDE_PROGRAM"), "set XULOOM_TEST_CLAUDE_PROGRAM for installed Claude")
class InstalledClaudeDiscovery(DiscoveryContract):
    async def test_discovery_and_restored_followup(self):
        await self.scenario("claude", os.environ["XULOOM_TEST_CLAUDE_PROGRAM"])
