"""Real saved receipts, SDK requests and CLI inspection; no remote model traffic."""
import asyncio
import json
import os
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch, AsyncMock

from xuloom.core.engine import QueryEngine
from xuloom.core.messages import create_user_message, create_assistant_message
from xuloom.core.run_result import RunResult
from xuloom.core.storage import atomic_json, session_directory
from xuloom.features.session_summary import human_turn, project_summary, read_summary, record_receipt, source_url
if __package__:
    from . import test_tool_recovery as recovery
else:
    import test_tool_recovery as recovery


class SummaryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = recovery.RecoveryTests.asyncSetUp
    asyncTearDown = recovery.RecoveryTests.asyncTearDown
    fixture = recovery.RecoveryTests.fixture

    def turn(self, identity):
        self.engine.messages.append(create_user_message("Task " + identity, msg_uuid=identity))
        self.engine._sync_message_context()

    async def call(self, name, arguments, identity="receipt"):
        return await self.engine._execute_tool_wrapper({"id": identity, "name": name, "input": arguments}, lambda _: None)

    async def test_saved_files_sources_tools_and_new_empty_turn(self):
        self.engine.permission_manager.approval_policy.set_mode("full"); self.turn("first")
        result = await self.call("file_write_tool", {"file_path": "report.md", "content": "REAL_OUTPUT"})
        self.assertFalse(result["is_error"])
        with patch("xuloom.tools.web_tool.DDGS") as search:
            search.return_value.text.return_value = [{"href": "https://example.org/paper", "title": "Actual returned paper", "body": "evidence"}]
            await self.call("web_search_tool", {"query": "research"}, "search")
        self.engine.messages.append(create_assistant_message([{ "type": "text", "text": "Sources: https://fabricated.invalid/claim"}]))
        self.engine.persist_session_state()
        summary = read_summary(self.root, self.engine.session_id)
        self.assertEqual(summary["turnId"], "first")
        self.assertEqual(summary["files"][0]["path"], str(self.root / "report.md"))
        self.assertEqual(summary["sources"][0]["url"], "https://example.org/paper")
        self.assertEqual(summary["sources"][0]["kind"], "search")
        self.assertNotIn("fabricated.invalid", json.dumps(summary))
        self.assertEqual(sum(tool["count"] for tool in summary["tools"]), 2)
        self.assertEqual(sum(tool["errors"] for tool in summary["tools"]), 0)
        self.turn("second"); self.engine.persist_session_state()
        self.assertEqual(read_summary(self.root, self.engine.session_id)["sources"], [])
        self.assertEqual(len(read_summary(self.root, self.engine.session_id, all_turns=True)["sources"]), 1)
        (self.root / "report.md").unlink()
        self.assertEqual(len(read_summary(self.root, self.engine.session_id, turn_id="first")["files"]), 1)

    async def test_failed_search_and_denied_write_are_not_successful_outputs(self):
        self.turn("failure")
        await self.call("file_write_tool", {"file_path": "not-written.txt", "content": "NO"})
        with patch("xuloom.tools.web_tool.DDGS", side_effect=RuntimeError("private diagnostic")):
            await self.call("web_search_tool", {"query": "failed"}, "search-failed")
        summary = read_summary(self.root, self.engine.session_id)
        self.assertEqual(summary["files"], []); self.assertEqual(summary["sources"], [])
        self.assertEqual(sum(row["errors"] for row in summary["tools"]), 2)
        self.assertNotIn("private diagnostic", json.dumps(summary))

    async def test_projection_survives_compacted_messages_without_adding_model_context(self):
        self.engine.permission_manager.approval_policy.set_mode("full")
        self.fixture([("web_search_tool", '{"query":"test"}'), "FINISHED"])
        with patch("xuloom.tools.web_tool.DDGS") as search:
            search.return_value.text.return_value = [{"href": "https://example.org/unique-source", "title": "SOURCE_TITLE", "body": "snippet"}]
            result = await self.engine.run("Research this", sys_print_callback=lambda _: None)
        self.assertTrue(result.ok)
        # The model sees search content once, not a second UI-only sources list.
        self.assertNotIn("- sources:", json.dumps(self.requests[1]["messages"]))
        before = read_summary(self.root, self.engine.session_id, all_turns=True)
        self.engine.messages = [create_user_message("Historical evidence projection", is_meta=True)]
        self.engine.persist_session_state()
        self.assertEqual(read_summary(self.root, self.engine.session_id, all_turns=True)["sources"], before["sources"])

    async def test_child_receipts_keep_original_parent_turn_while_parent_moves_on(self):
        self.engine.permission_manager.approval_policy.set_mode("full"); self.turn("original")
        ready, gate = asyncio.Event(), asyncio.Event()
        async def run(child, **kwargs):
            ready.set(); await gate.wait()
            receipt = await child._execute_tool_wrapper({"id": "child-write", "name": "file_write_tool",
                "input": {"file_path": "child.md", "content": "CHILD_OUTPUT"}}, lambda _: None)
            self.assertFalse(receipt["is_error"], receipt)
            return RunResult("saved", "completed", True)
        with patch.object(QueryEngine, "run", run):
            receipt = json.loads(await self.engine.available_tools["agent_tool"].execute(prompt="independent", run_in_background=True, response_format="json"))
            await asyncio.wait_for(ready.wait(), 5)
            self.turn("later"); self.engine.persist_session_state(); gate.set()
            await self.engine.tool_context["subagent_tasks"].tasks[receipt["agents"][0]["agent_id"]]
        self.assertEqual(read_summary(self.root, self.engine.session_id)["files"], [])
        self.assertEqual(read_summary(self.root, self.engine.session_id, turn_id="original")["files"][0]["name"], "child.md")

    async def test_running_child_checkpoint_is_visible_before_parent_final_save(self):
        self.turn("parent"); self.engine.require_writer(); self.engine.persist_session_state()
        child_id = "subagent-proof"
        references = {}
        record_receipt(references, turn_id="parent", agent_id=child_id, tool_name="web_search_tool", tool_use_id="saved",
            status="completed", result={"ok": True, "metadata": {"sources": [{"url": "https://example.org/child", "kind": "search"}]}})
        atomic_json(session_directory(self.root) / "subagents" / (child_id + ".json"), {"metadata": {
            "delegation": {"owner_session_id": self.engine.session_id}, "artifacts": {"references": references}}})
        self.engine.subagent_registry.append({"agent_id": child_id, "status": "interrupted", "owner_turn_id": "parent"})
        self.engine.persist_session_state()
        self.assertEqual(read_summary(self.root, self.engine.session_id)["sources"][0]["agentId"], child_id)
        atomic_json(session_directory(self.root) / "subagents" / (child_id + ".json"), {"metadata": {
            "delegation": {"owner_session_id": "another-session"}, "artifacts": {"references": references}}})
        self.assertEqual(read_summary(self.root, self.engine.session_id)["sources"], [])

    async def test_nested_dispatch_keeps_root_turn_identity(self):
        self.engine.permission_manager.approval_policy.set_mode("full"); self.turn("child-local")
        self.engine.tool_context["delegation"] = {"owner_turn_id": "root-human", "owner_session_id": "root-session"}
        async def run(child, **kwargs):
            await child._execute_tool_wrapper({"id": "nested", "name": "file_write_tool",
                "input": {"file_path": "nested.md", "content": "NESTED"}}, lambda _: None)
            return RunResult("saved", "completed", True)
        with patch.object(QueryEngine, "run", run):
            receipt = json.loads(await self.engine.available_tools["agent_tool"].execute(
                prompt="nested", run_in_background=False, response_format="json"))
        self.assertEqual(receipt["agents"][0]["status"], "completed")
        self.assertIn("root-human", self.engine.artifact_tracker.references)
        self.assertNotIn("child-local", self.engine.artifact_tracker.references)

    async def test_cli_read_needs_no_model_key_and_invalid_ids_do_not_escape_history(self):
        self.turn("cli-turn"); self.engine.require_writer(); self.engine.persist_session_state()
        process = await asyncio.create_subprocess_exec(sys.executable, "-m", "xuloom", "summary", "--cwd", str(self.root),
            "--session", self.engine.session_id, "--json", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": "", "XULOOM_CONFIG_DIR": str(self.root / "blank-config")})
        out, err = await asyncio.wait_for(process.communicate(), 30)
        self.assertEqual(process.returncode, 0, err.decode())
        summary = json.loads(out)["result"]["summary"]
        self.assertEqual(summary["turnId"], "cli-turn")
        with self.assertRaises(ValueError): read_summary(self.root, "../private")
        with self.assertRaises(ValueError): read_summary(self.root, self.engine.session_id, turn_id="../private")

    async def test_branch_and_edit_remove_later_references_but_keep_old_records(self):
        from xuloom.core.session_branch import branch_snapshot
        from xuloom.desktop.prompt_edit import replace_latest_turn
        self.engine.permission_manager.approval_policy.set_mode("full")
        for identity in ("earlier", "later"):
            self.turn(identity)
            await self.call("file_write_tool", {"file_path": identity + ".md", "content": identity}, identity)
            self.engine.messages.append(create_assistant_message([{ "type": "text", "text": identity + " answer"}]))
        raw = {"messages": self.engine.messages, "metadata": self.engine._build_session_metadata()}
        messages, metadata = branch_snapshot(raw, {"turn": 1, "text": "earlier answer", "userId": "earlier"}, self.engine.session_id)
        summary = project_summary({"messages": messages, "metadata": metadata}, self.root, all_turns=True)
        self.assertEqual([item["name"] for item in summary["files"]], ["earlier.md"])
        replace_latest_turn(self.engine, "later")
        summary = read_summary(self.root, self.engine.session_id, all_turns=True)
        self.assertEqual([item["name"] for item in summary["files"]], ["earlier.md"])
        self.assertTrue((self.root / "later.md").is_file())

    async def test_untrusted_and_legacy_text_do_not_become_sources(self):
        self.assertEqual(human_turn([create_user_message("first", msg_uuid="actual"), {"role": "user", "content": "synthetic"}]), "actual")
        for url in ("javascript:alert(1)", "file:///private", "https://user:secret@example.org/", "https://example.org/\nX", "https://bad:invalid/", "https://example.org\\evil"):
            self.assertIsNone(source_url(url))
        self.turn("legacy")
        metadata = {"turn_traces": [{"userMessageId": "legacy", "files": [], "steps": [{"kind": "tool", "tool": {
            "name": "web_search_tool", "status": "completed", "result": "https://not-structured.invalid"}}]}]}
        summary = project_summary({"messages": self.engine.messages, "metadata": metadata}, self.root)
        self.assertTrue(summary["legacySourcesUnavailable"]); self.assertEqual(summary["sources"], [])
        self.assertEqual(summary["tools"][0]["count"], 1)

    async def test_mcp_resource_links_are_sources_but_text_urls_and_file_uris_are_not(self):
        from xuloom.protocols.mcp_bridge import MCPToolWrapper
        self.engine.permission_manager.approval_policy.set_mode("full"); self.turn("mcp")
        response = SimpleNamespace(content=[
            SimpleNamespace(type="text", text="Fake reference https://body-only.invalid/claim"),
            SimpleNamespace(type="resource_link", uri="https://example.org/resource", name="Actual resource", title=None),
            SimpleNamespace(type="resource_link", uri="file:///private/local", name="No local link", title=None),
        ], isError=False)
        wrapper = MCPToolWrapper("fixture", SimpleNamespace(call_tool=AsyncMock(return_value=response)),
            SimpleNamespace(name="sources", description="Resource fixture", inputSchema={"type": "object"}))
        self.engine.available_tools[wrapper.name] = wrapper
        receipt = await self.call(wrapper.name, {})
        self.assertFalse(receipt["is_error"], receipt)
        summary = read_summary(self.root, self.engine.session_id)
        self.assertEqual([item["url"] for item in summary["sources"]], ["https://example.org/resource"])
        self.assertEqual(summary["sources"][0]["kind"], "resource")

    async def test_move_keeps_references_and_does_not_move_workspace_outputs(self):
        from xuloom.core.session_mutation import mutate
        self.engine.permission_manager.approval_policy.set_mode("full"); self.turn("move")
        await self.call("file_write_tool", {"file_path": "original.md", "content": "KEEP_HERE"})
        target = self.root / "destination"; target.mkdir()
        self.engine.session_manager.release_writer()
        mutate({"action": "move", "project": str(self.root), "target": str(target), "session_id": self.engine.session_id,
            "scope": "project", "title": "Moved", "created": 123})
        summary = read_summary(target, self.engine.session_id)
        self.assertEqual(summary["files"][0]["path"], str(self.root / "original.md"))
        self.assertTrue((self.root / "original.md").exists()); self.assertFalse((target / "original.md").exists())

    async def test_save_failure_preserves_previous_summary_without_claiming_saved_results(self):
        from xuloom.core.run_result import PersistenceError
        self.engine.permission_manager.approval_policy.set_mode("full"); self.turn("save-failure")
        self.engine.require_writer(); self.engine.persist_session_state()
        save = self.engine.persist_session_state
        def broken_save():
            if self.engine.artifact_tracker.references:
                self.engine.session_manager.last_save_error = "fixture storage failure"; return False
            return save()
        with patch.object(self.engine, "persist_session_state", broken_save):
            with self.assertRaises(PersistenceError):
                await self.call("file_write_tool", {"file_path": "effect.md", "content": "ALREADY_WRITTEN"})
        self.assertTrue((self.root / "effect.md").exists())
        self.assertEqual(read_summary(self.root, self.engine.session_id)["files"], [])

    async def test_legacy_cli_and_malformed_reference_rows_do_not_leak_unknown_fields(self):
        self.turn("old-cli")
        self.engine.messages.extend([
            {"role": "assistant", "content": [{"type": "tool_use", "id": "old", "name": "web_search_tool", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "old", "content": "https://not-a-typed-source.invalid"}]},
        ])
        summary = project_summary({"messages": self.engine.messages, "metadata": {}}, self.root)
        self.assertTrue(summary["legacySourcesUnavailable"]); self.assertEqual(summary["tools"][0]["count"], 1)
        references = {}
        record_receipt(references, turn_id="old-cli", agent_id="main", tool_name="web_search_tool", tool_use_id="real",
            status="ok", result={"ok": True, "metadata": {"sources": [{"url": "https://example.org/valid"}]}})
        references["old-cli"]["main"]["sources"]["https://example.org/valid"]["api_key"] = "DO_NOT_EXPOSE"
        metadata = {"artifacts": {"references": references}, "subagents": [False]}
        self.engine.require_writer()
        self.assertTrue(self.engine.session_manager.save_session_state(self.engine.session_id, self.engine.messages, metadata))
        summary = read_summary(self.root, self.engine.session_id)
        self.assertNotIn("DO_NOT_EXPOSE", json.dumps(summary))
        self.assertEqual(len(summary["sources"]), 1); self.assertTrue(summary["warnings"])

    async def test_legacy_repeated_call_ids_are_counted_in_their_own_turns(self):
        messages = []
        for turn in ("one", "two"):
            messages += [create_user_message("task", msg_uuid=turn),
                {"role": "assistant", "content": [{"type": "tool_use", "id": "reused", "name": "web_search_tool", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "reused", "content": "saved"}]}]
        summary = project_summary({"messages": messages, "metadata": {}}, self.root, all_turns=True)
        self.assertEqual(summary["tools"][0]["count"], 2)

    async def test_custom_metadata_only_sources_remain_available_to_the_model(self):
        from xuloom.context.tool_results import build_tool_result, serialize_tool_result
        result = build_tool_result(ok=True, content="Evidence returned by an extension", metadata={
            "sources": [{"url": "https://example.org/metadata-only", "title": "Metadata source"}]})
        self.assertIn("https://example.org/metadata-only", serialize_tool_result(result))

    async def test_malformed_legacy_display_fields_keep_real_saved_references(self):
        self.engine.permission_manager.approval_policy.set_mode("full"); self.turn("real")
        await self.call("file_write_tool", {"file_path": "real.md", "content": "SAVED"})
        raw = {"messages": self.engine.messages, "metadata": self.engine._build_session_metadata()}
        raw["metadata"].update(file_changes=None, turn_traces=[{
            "userMessageId": "old", "files": None, "steps": 42}])
        summary = project_summary(raw, self.root, all_turns=True)
        self.assertEqual([file["name"] for file in summary["files"]], ["real.md"])
        self.assertEqual(summary["tools"][0]["count"], 1)
