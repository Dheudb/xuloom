"""Actual opt-in Pi CLI with native queue, loopback API, owner tools and receipts."""
import asyncio
import base64
import json
import io
import os
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

if __package__:
    from . import test_external_agents as external
else:
    import test_external_agents as external
from xuloom.agents.external.program import resolve
from xuloom.core.storage import IOPath

PROGRAM = os.environ.get("XULOOM_TEST_PI_PROGRAM")


@unittest.skipUnless(external.MCP and PROGRAM, "set XULOOM_TEST_PI_PROGRAM for installed Pi / localhost contracts")
class InstalledPiTests(external.ExternalFixture):
    def role(self, tools="Read"):
        folder = self.root / ".xuloom/agents"; folder.mkdir(parents=True)
        (folder / "external-reader.md").write_text("---\nname: external-reader\ndescription: independent evidence\nbackend: pi\nbackend_program: " + str(PROGRAM).replace("\\", "/") + "\ntools: [" + tools + "]\n---\nUse the assigned native tool.\n", encoding="utf8")

    async def spawn(self, engine, **values):
        result = await engine.agents("spawn", prompt="Read evidence.txt", subagent_type="external-reader", **values)
        self.assertTrue(result["ok"], result)
        return result["data"]["agents"][0]

    async def test_real_rpc_worker_and_new_process_followup_preserve_identity_and_budget(self):
        self.role(); engine = self.engine()
        row = await self.spawn(engine); self.assertEqual(row["status"], "completed", row)
        state = engine.session_manager.load_subagent_transcript(row["agent_id"])
        identity = state["metadata"]["external_agent"]["session_id"]
        second = await engine.agents("followup", agent_id=row["agent_id"], prompt="Explain\u2028the\u2029evidence briefly")
        self.assertEqual(second["data"]["status"], "completed", second)
        after = engine.session_manager.load_subagent_transcript(row["agent_id"])
        self.assertEqual(after["metadata"]["external_agent"]["session_id"], identity)
        self.assertEqual(after["metadata"]["external_agent"]["backend"], "pi")
        self.assertEqual(len(self.records), 3)
        self.assertIn("Explain\\u2028the\\u2029evidence briefly", json.dumps(self.records[-1]["body"]))
        self.assertEqual(engine.tool_context["collaboration_run"]["output_used"], 15)
        self.assertTrue(all(record["headers"]["x-api-key"] == "fixture-only" for record in self.records))
        self.assertEqual(len(after["metadata"]["tool_activity"]["recent"]), 1)

    async def test_unknown_builtin_is_rejected_and_no_native_fallback_occurs(self):
        self.role(); self.mode = "builtin"; engine = self.engine()
        row = await self.spawn(engine)
        self.assertEqual(row["status"], "crashed", row)
        self.assertEqual(row["receipt"]["reason"], "provider_error")
        self.assertEqual(len(self.records), 1)

    async def test_openai_rpc_uses_native_tools_credentials_and_per_request_budget(self):
        self.role(); engine = self.engine("openai")
        row = await self.spawn(engine)
        self.assertEqual(row["status"], "completed", row)
        self.assertEqual(len(self.records), 2)
        self.assertTrue(all(r["headers"]["authorization"] == "Bearer fixture-only" for r in self.records))
        self.assertEqual(engine.tool_context["collaboration_run"]["output_used"], 10)
        self.assertIn("OWNED_EVIDENCE", json.dumps(self.records[-1]["body"]))
        state = engine.session_manager.load_subagent_transcript(row["agent_id"])
        self.assertEqual(state["metadata"]["external_agent"]["seen_tool_ids"], ["call_fixture"])
        self.assertEqual(len(state["metadata"]["tool_activity"]["recent"]), 1)
        again = await engine.agents("followup", agent_id=row["agent_id"], prompt="Explain again")
        self.assertEqual(again["data"]["status"], "completed", again)

    async def test_openai_truncated_tool_stream_never_executes_a_tool(self):
        self.role(); self.mode = "truncated"; engine = self.engine("openai")
        row = await self.spawn(engine)
        self.assertEqual(row["status"], "crashed", row)
        state = engine.session_manager.load_subagent_transcript(row["agent_id"])
        self.assertFalse(state["metadata"]["tool_activity"]["recent"])
        self.assertEqual(len(self.records), 1)

    async def test_desktop_authored_pi_role_runs_through_the_same_native_queue(self):
        from xuloom.desktop.capability_config import create_definition
        from xuloom.skills.registry import SkillRegistry
        create_definition(SkillRegistry(cwd=self.root, home=self.root), {"kind": "agents", "scope": "project",
            "name": "external-reader", "description": "Read the assigned evidence", "prompt": "Use the supplied native tool.",
            "backend": "pi", "backend_program": str(PROGRAM), "tools": "Read"})
        engine = self.engine("openai")
        row = await self.spawn(engine)
        self.assertEqual(row["status"], "completed", row)
        self.assertEqual(len(self.records), 2)

    async def test_openai_tool_image_reaches_selected_provider_in_wire_format(self):
        self.role(); self.mode = "image"; engine = self.engine("openai")
        (self.root / "evidence.png").write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j5i8AAAAASUVORK5CYII="))
        row = await self.spawn(engine)
        self.assertEqual(row["status"], "completed", row)
        self.assertEqual(len(self.records), 2)
        self.assertIn("data:image/png;base64,", json.dumps(self.records[-1]["body"]))

    async def test_explicit_disabled_thinking_overrides_external_anthropic_defaults(self):
        self.role()
        profile = {"adapter": "anthropic", "thinking": "disabled", "canDisable": True, "efforts": ["low", "high"], "effort": "high"}
        engine = self.engine(model_contract={"inputs": ["text"], "reasoning": profile})
        row = await self.spawn(engine)
        self.assertEqual(row["status"], "completed", row)
        for record in self.records:
            self.assertEqual(record["body"]["thinking"], {"type": "disabled"})
            self.assertNotIn("effort", record["body"].get("output_config", {}))

    async def test_deepseek_thinking_and_effort_follow_the_native_connection(self):
        self.role()
        profile = {"adapter": "deepseek", "thinking": "enabled", "canDisable": True, "efforts": ["low", "high"], "effort": "high"}
        engine = self.engine("openai", model_contract={"inputs": ["text"], "reasoning": profile})
        row = await self.spawn(engine)
        self.assertEqual(row["status"], "completed", row)
        for record in self.records:
            self.assertEqual(record["body"]["thinking"], {"type": "enabled"})
            self.assertEqual(record["body"]["reasoning_effort"], "high")

    async def test_missing_history_does_not_create_blank_same_identity_session(self):
        self.role(); engine = self.engine()
        row = await self.spawn(engine); self.assertEqual(row["status"], "completed", row)
        state = engine.session_manager.load_subagent_transcript(row["agent_id"])
        path = IOPath(state["metadata"]["external_agent"]["session_file"])
        self.assertTrue(path.resolve().is_relative_to(IOPath(self.root).resolve()))
        path.unlink()
        result = await engine.agents("followup", agent_id=row["agent_id"], prompt="Continue")
        self.assertEqual(result["data"]["status"], "crashed", result)
        self.assertEqual(result["data"]["receipt"]["reason"], "external_session_missing")
        self.assertEqual(len(self.records), 2)

    async def test_parent_denial_still_controls_actual_mcp_execution(self):
        self.role(); engine = self.engine(); engine.permission_manager.always_deny_tools.add("file_read_tool")
        row = await self.spawn(engine)
        self.assertEqual(row["status"], "completed", row)
        self.assertNotIn("OWNED_EVIDENCE", json.dumps(self.records))
        state = engine.session_manager.load_subagent_transcript(row["agent_id"])
        self.assertTrue(state["metadata"]["tool_activity"]["recent"][0]["tool_result"]["is_error"])

    async def test_unknown_model_does_not_silently_drop_native_tool_image(self):
        self.role(); self.mode = "image"; engine = self.engine()
        self.assertIsNone(engine._input_contract(external.MODEL))
        png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j5i8AAAAASUVORK5CYII="
        (self.root / "evidence.png").write_bytes(base64.b64decode(png))
        row = await self.spawn(engine)
        self.assertEqual(row["status"], "completed", row)
        self.assertEqual(len(self.records), 2)
        self.assertIn('"media_type": "image/png"', json.dumps(self.records[-1]["body"]))
        sources = [part["source"] for message in self.records[-1]["body"]["messages"]
            for block in message.get("content", []) if isinstance(block, dict) and block.get("type") == "tool_result"
            for part in block.get("content", []) if part.get("type") == "image"]
        from PIL import Image
        self.assertEqual(len(sources), 1)
        with Image.open(io.BytesIO(base64.b64decode(sources[0]["data"]))) as received:
            self.assertEqual(received.size, (1, 1))

    async def test_actual_cli_startup_does_not_execute_workspace_fsmonitor(self):
        for args in (("init", "-b", "main"), ("add", "evidence.txt"),
                ("-c", "user.name=Fixture", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture")):
            subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)
        marker = self.root / "unexpected-git-hook"
        hook = self.root / ".git/monitor.sh"
        hook.write_text("#!/bin/sh\necho started >> " + shlex.quote(marker.as_posix()) + '\nprintf "fixture\\0"\n', encoding="utf8")
        hook.chmod(0o755)
        subprocess.run(["git", "-C", str(self.root), "config", "core.fsmonitor", hook.as_posix()], check=True, capture_output=True)
        self.role(); engine = self.engine()
        self.assertFalse(marker.exists())
        row = await self.spawn(engine)
        self.assertEqual(row["status"], "completed", row)
        self.assertFalse(marker.exists(), "External startup executed an unapproved repository hook")

    async def test_explicit_text_only_parent_contract_is_inherited_by_worker(self):
        self.role(); self.mode = "image"
        engine = self.engine(model_contract={"inputs": ["text"], "reasoning": None})
        (self.root / "evidence.png").write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j5i8AAAAASUVORK5CYII="))
        row = await self.spawn(engine)
        self.assertEqual(row["status"], "completed", row)
        self.assertNotIn('"media_type": "image/png"', json.dumps(self.records))
        state = engine.session_manager.load_subagent_transcript(row["agent_id"])
        result = state["metadata"]["tool_activity"]["recent"][0]["tool_result"]
        self.assertTrue(result["is_error"], result)

    async def test_confirmed_owner_stop_reaps_its_node_and_followup_keeps_history(self):
        self.role(); self.mode = "hold"; engine = self.engine(); processes = []
        create = asyncio.create_subprocess_exec
        async def capture(*args, **kwargs):
            process = await create(*args, **kwargs); processes.append(process); return process
        with patch("asyncio.create_subprocess_exec", capture):
            row = await self.spawn(engine, run_in_background=True)
            async def entered():
                while not self.records: await asyncio.sleep(.02)
            await asyncio.wait_for(entered(), 15)
            result = await engine.agents("cancel", agent_id=row["agent_id"])
            self.assertEqual(result["data"]["status"], "cancelled")
            self.assertTrue(result["data"]["external_recovery"]["resumable"], result)
        self.assertTrue(all(p.returncode is not None for p in processes))
        state = engine.session_manager.load_subagent_transcript(row["agent_id"])
        checkpoint = state["metadata"]["external_agent"]
        self.assertEqual(checkpoint["phase"], "idle", checkpoint)
        self.assertTrue(checkpoint["stop_checkpoint"]["confirmed"])
        self.release.set(); self.mode = "normal"
        resumed = await engine.agents("followup", agent_id=row["agent_id"], prompt="Continue")
        self.assertEqual(resumed["data"]["status"], "completed", resumed)
        after = engine.session_manager.load_subagent_transcript(row["agent_id"])["metadata"]["external_agent"]
        self.assertEqual(after["session_id"], checkpoint["session_id"])
        self.assertNotIn("stop_checkpoint", after)
        self.assertEqual(len(self.records), 3)

    async def wait_for_requests(self, count):
        async def entered():
            while len(self.records) < count: await asyncio.sleep(.02)
        await asyncio.wait_for(entered(), 15)

    async def test_followup_opens_the_saved_file_without_project_id_lookup(self):
        self.role(); engine = self.engine()
        row = await self.spawn(engine)
        self.assertEqual(row["status"], "completed", row)
        checkpoint = engine.session_manager.load_subagent_transcript(row["agent_id"])["metadata"]["external_agent"]
        identity = engine.session_id
        await engine.aclose()
        restored = self.engine(); self.assertTrue(restored.load_session(identity))
        # Formal CLI uses this spelling for Windows filesystem operations.
        restored.session_cwd = os.fspath(IOPath(restored.session_cwd))
        commands = []
        create = asyncio.create_subprocess_exec
        async def capture(*args, **kwargs):
            if "--mode" in args: commands.append(args)
            return await create(*args, **kwargs)
        with patch("asyncio.create_subprocess_exec", capture):
            result = await restored.agents("followup", agent_id=row["agent_id"], prompt="Continue")
        self.assertEqual(result["data"]["status"], "completed", result)
        self.assertEqual(len(commands), 1)
        self.assertNotIn("--session-id", commands[0])
        self.assertEqual(IOPath(commands[0][commands[0].index("--session") + 1]).resolve(), IOPath(checkpoint["session_file"]).resolve())
        after = restored.session_manager.load_subagent_transcript(row["agent_id"])["metadata"]["external_agent"]
        self.assertEqual(IOPath(after["session_file"]).resolve(), IOPath(checkpoint["session_file"]).resolve())
        self.assertEqual(len(self.records), 3)

    async def test_stop_after_file_effect_and_parent_restart_does_not_repeat_the_write(self):
        self.role("Write"); self.mode = "hold_after_write"; engine = self.engine()
        tool_type = type(engine.available_tools["file_write_tool"])
        execute, calls = tool_type.execute, []
        async def tracked(tool, **values):
            calls.append(values)
            return await execute(tool, **values)
        with patch.object(tool_type, "execute", tracked):
            row = await self.spawn(engine, run_in_background=True)
            await self.wait_for_requests(2)
            self.assertEqual((self.root / "created-once.txt").read_text(encoding="utf8"), "CREATED_ONCE")
            stopped = await engine.agents("cancel", agent_id=row["agent_id"])
            self.assertTrue(stopped["data"]["external_recovery"]["resumable"], stopped)
            checkpoint = engine.session_manager.load_subagent_transcript(row["agent_id"])["metadata"]["external_agent"]
            self.assertEqual(checkpoint["stop_checkpoint"]["tool_ids"], ["toolu_contract"])
            self.release.set(); self.mode = "normal"
            identity = engine.session_id
            await engine.aclose()
            restored = self.engine(); self.assertTrue(restored.load_session(identity))
            result = await restored.agents("followup", agent_id=row["agent_id"], prompt="Continue from the confirmed result")
            self.assertEqual(result["data"]["status"], "completed", result)
            self.assertEqual(len(calls), 1, calls)
            after = restored.session_manager.load_subagent_transcript(row["agent_id"])["metadata"]["external_agent"]
            self.assertEqual(after["session_id"], checkpoint["session_id"])
        self.assertEqual(len(self.records), 3)

    async def test_changed_history_after_confirmed_stop_is_rejected_before_model_call(self):
        self.role(); self.mode = "hold_after_tool"; engine = self.engine()
        row = await self.spawn(engine, run_in_background=True)
        await self.wait_for_requests(2)
        result = await engine.agents("cancel", agent_id=row["agent_id"])
        self.assertTrue(result["data"]["external_recovery"]["resumable"], result)
        checkpoint = engine.session_manager.load_subagent_transcript(row["agent_id"])["metadata"]["external_agent"]
        path = IOPath(checkpoint["session_file"])
        self.assertTrue(path.resolve().is_relative_to(IOPath(self.root).resolve()))
        with path.open("ab") as stream: stream.write(b"\n")
        self.release.set(); self.mode = "normal"
        result = await engine.agents("followup", agent_id=row["agent_id"], prompt="Continue")
        self.assertEqual(result["data"]["receipt"]["reason"], "external_history_changed", result)
        self.assertEqual(len(self.records), 2)

    async def test_out_of_scope_history_reference_is_not_fingerprinted_or_read(self):
        self.role(); engine = self.engine()
        row = await self.spawn(engine)
        self.assertEqual(row["status"], "completed", row)
        raw = engine.session_manager.load_subagent_transcript(row["agent_id"])
        outside = self.root / "outside-owned-history.txt"
        outside.write_text("OUTSIDE_SENTINEL", encoding="utf8")
        raw["metadata"]["external_agent"]["session_file"] = str(outside)
        self.assertTrue(engine.session_manager.save_subagent_transcript(row["agent_id"], raw))
        with patch("xuloom.agents.external.pi._hash") as fingerprint:
            result = await engine.agents("followup", agent_id=row["agent_id"], prompt="Continue")
        self.assertEqual(result["data"]["receipt"]["reason"], "external_history_changed", result)
        fingerprint.assert_not_called()
        self.assertEqual(len(self.records), 2)

    async def test_unacknowledged_native_receipt_keeps_stop_uncertain(self):
        from xuloom.agents.external.host import LocalHost
        self.role(); self.mode = "hold_after_tool"; engine = self.engine(); hosts = []
        initialize = LocalHost.__init__
        def capture(host, *args, **values):
            initialize(host, *args, **values); hosts.append(host)
        with patch.object(LocalHost, "__init__", capture):
            row = await self.spawn(engine, run_in_background=True)
            await self.wait_for_requests(2)
            hosts[-1].receipts["toolu_contract"] = ("file_read_tool", "unacknowledged")
            stopped = await engine.agents("cancel", agent_id=row["agent_id"])
        self.assertFalse(stopped["data"]["external_recovery"]["resumable"], stopped)
        checkpoint = engine.session_manager.load_subagent_transcript(row["agent_id"])["metadata"]["external_agent"]
        self.assertEqual(checkpoint["phase"], "interrupted")
        self.assertNotIn("stop_checkpoint", checkpoint)
        self.release.set(); self.mode = "normal"
        result = await engine.agents("followup", agent_id=row["agent_id"], prompt="Continue")
        self.assertEqual(result["data"]["receipt"]["reason"], "external_outcome_unknown", result)
        self.assertEqual(len(self.records), 2)

    async def test_crashed_rpc_without_abort_receipt_is_not_automatically_replayed(self):
        self.role(); self.mode = "hold"; engine = self.engine(); processes = []
        create = asyncio.create_subprocess_exec
        async def capture(*args, **kwargs):
            process = await create(*args, **kwargs)
            if "--mode" in args: processes.append(process)
            return process
        with patch("asyncio.create_subprocess_exec", capture):
            row = await self.spawn(engine, run_in_background=True)
            await self.wait_for_requests(1)
            self.assertEqual(len(processes), 1)
            processes[0].kill(); await processes[0].wait()
            result = await engine.agents("wait", agent_ids=[row["agent_id"]], timeout_ms=10000)
        self.assertEqual(result["data"]["agents"][0]["status"], "crashed", result)
        self.release.set(); self.mode = "normal"
        result = await engine.agents("followup", agent_id=row["agent_id"], prompt="Continue")
        self.assertEqual(result["data"]["receipt"]["reason"], "external_outcome_unknown", result)
        self.assertEqual(len(self.records), 1)

    async def test_stop_checkpoint_storage_failure_does_not_advertise_recovery(self):
        from xuloom.core.run_result import PersistenceError
        self.role(); self.mode = "hold_after_tool"; engine = self.engine()
        row = await self.spawn(engine, run_in_background=True)
        await self.wait_for_requests(2)
        with patch("xuloom.agents.external.pi.verify_file", side_effect=PersistenceError("fixture disk failure")):
            stopped = await engine.agents("cancel", agent_id=row["agent_id"])
        self.assertEqual(stopped["data"]["status"], "crashed", stopped)
        checkpoint = engine.session_manager.load_subagent_transcript(row["agent_id"])["metadata"]["external_agent"]
        self.assertEqual(checkpoint["phase"], "interrupted")
        self.assertNotIn("stop_checkpoint", checkpoint)
        self.release.set(); self.mode = "normal"
        result = await engine.agents("followup", agent_id=row["agent_id"], prompt="Continue")
        self.assertEqual(result["data"]["receipt"]["reason"], "external_outcome_unknown", result)
        self.assertEqual(len(self.records), 2)

    async def test_effect_finished_after_rpc_abort_cannot_be_silently_replayed(self):
        self.role("Write"); self.mode = "hold_after_write"; engine = self.engine()
        tool_type = type(engine.available_tools["file_write_tool"])
        execute = tool_type.execute
        effect_done, release_result, abort_acknowledged = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []
        async def tracked(tool, **values):
            calls.append(values)
            result = await execute(tool, **values)
            effect_done.set()
            await release_result.wait()
            return result
        def decode(data):
            row = json.loads(data)
            if isinstance(row, dict) and row.get("type") == "response" and row.get("command") == "abort" and row.get("success"):
                abort_acknowledged.set()
            return row
        with patch.object(tool_type, "execute", tracked), patch("xuloom.agents.external.pi.json", SimpleNamespace(loads=decode, dumps=json.dumps)):
            row = await self.spawn(engine, run_in_background=True)
            await asyncio.wait_for(effect_done.wait(), 15)
            self.assertTrue((self.root / "created-once.txt").is_file())
            cancelling = asyncio.create_task(engine.agents("cancel", agent_id=row["agent_id"]))
            try:
                await asyncio.wait_for(abort_acknowledged.wait(), 10)
            finally: release_result.set()
            stopped = await cancelling
        self.assertFalse(stopped["data"]["external_recovery"]["resumable"], stopped)
        self.release.set(); self.mode = "normal"
        result = await engine.agents("followup", agent_id=row["agent_id"], prompt="Continue")
        self.assertEqual(result["data"]["receipt"]["reason"], "external_outcome_unknown", result)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.records), 1)

    async def test_second_stop_cancels_the_handshake_without_leaving_a_recovery_task(self):
        from xuloom.agents.external.host import LocalHost
        self.role(); self.mode = "hold"; engine = self.engine(); hosts = []
        initialize = LocalHost.__init__
        def capture(host, *args, **values):
            initialize(host, *args, **values); hosts.append(host)
        entered, settled = asyncio.Event(), asyncio.Event()
        async def delayed():
            entered.set()
            try: await asyncio.Future()
            finally: settled.set()
        with patch.object(LocalHost, "__init__", capture):
            row = await self.spawn(engine, run_in_background=True)
            await self.wait_for_requests(1)
            hosts[-1].on_interrupt = delayed
            cancelling = asyncio.create_task(engine.agents("cancel", agent_id=row["agent_id"]))
            await asyncio.wait_for(entered.wait(), 10)
            engine.tool_context["subagent_tasks"].tasks[row["agent_id"]].cancel()
            stopped = await asyncio.wait_for(cancelling, 10)
        self.assertTrue(settled.is_set())
        self.assertFalse(stopped["data"]["external_recovery"]["resumable"], stopped)
        checkpoint = engine.session_manager.load_subagent_transcript(row["agent_id"])["metadata"]["external_agent"]
        self.assertEqual(checkpoint["phase"], "interrupted")
        self.assertNotIn("stop_checkpoint", checkpoint)


class PiProgramTests(unittest.TestCase):
    def test_nonpackage_script_cannot_be_implicitly_treated_as_installed_pi(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "script.js"; path.write_text("throw new Error('not a registered package')", encoding="utf8")
            with self.assertRaises(ValueError): resolve(str(path), backend="pi", workspace=directory)


if __name__ == "__main__": unittest.main()
