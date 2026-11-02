"""Real engine boundaries with owned local SDK streams; no remote model calls."""
import asyncio
import copy
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from xuloom.core import continuation as native
from xuloom.core.messages import create_assistant_message, create_user_message
from xuloom.core.run_result import PersistenceError
from tests import test_tool_sequence as sequence
from tests import test_tool_recovery as recovery


class NativeContinuationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = sequence.ToolSequenceTests.asyncSetUp
    asyncTearDown = recovery.RecoveryTests.asyncTearDown
    fixture = recovery.RecoveryTests.fixture
    run_task = recovery.RecoveryTests.run_task

    def node(self, identity="next", value=2, depends=None, equals=1):
        return {"id": identity, "tool": "sequence_probe", "arguments": {"value": value},
            "depends_on": depends or ["root"], "when": [{"step": "root", "path": ["value"], "equals": equals}]}

    def root_call(self, nodes=None):
        return {"type": "tool_use", "id": "owned-native-root", "name": "sequence_probe",
            "input": {"value": 1, native.FIELD: {"nodes": [self.node()] if nodes is None else nodes}}}

    async def execute_root(self, nodes=None):
        root = self.root_call(nodes)
        self.engine.messages.append(create_assistant_message([root]))
        return await self.engine._execute_tool_wrapper(root, lambda _: None)

    async def advance(self):
        await native.advance(self.engine, turn=2, event_callback=None, sys_print_callback=lambda _: None)

    async def test_normal_tool_metadata_runs_before_next_actor_request(self):
        third = self.node("last", 3, ["next"])
        third["when"] = [{"step": "next", "path": ["value"], "equals": 2}]
        self.fixture([("sequence_probe", json.dumps(self.root_call([self.node(), third])["input"])), "DONE"])
        self.assertEqual(await self.run_task(), "DONE")
        self.assertEqual(self.calls, [1, 2, 3])
        self.assertEqual(len(self.requests), 2)
        graph = self.engine.tool_context[native.KEY]
        self.assertEqual(graph["status"], "exhausted")
        self.assertEqual(len(graph["certificates"]), 2)
        self.assertTrue(all(c["reasoning_request_issued"] is False for c in graph["certificates"]))
        self.assertEqual(self.engine.session_token_usage["input_tokens"], 20)
        self.assertIn("continuation-", str(self.requests[1]["messages"]))

    async def test_no_declaration_retains_original_model_loop(self):
        self.fixture([("sequence_probe", '{"value":1}'), ("sequence_probe", '{"value":2}'), "DONE"])
        self.assertEqual(await self.run_task(), "DONE")
        self.assertEqual(self.calls, [1, 2])
        self.assertEqual(len(self.requests), 3)

    async def test_schema_is_optional_and_does_not_mutate_native_handler(self):
        original = self.engine.available_tools["sequence_probe"].get_tool_definition()
        native_schema = native.schema(original)["input_schema"]
        self.assertIn(native.FIELD, native_schema["properties"])
        self.assertNotIn(native.FIELD, native_schema.get("required", []))
        self.assertNotIn(native.FIELD, original["input_schema"]["properties"])
        self.assertFalse(self.engine._is_concurrency_safe("sequence_probe", self.root_call()["input"]))
        await self.execute_root()
        self.assertEqual(self.calls, [1])

    async def test_false_branch_skips_descendants_and_activates_other_branch(self):
        no = self.node("not_taken", 20, equals=0)
        child = self.node("also_skipped", 30, ["not_taken"])
        await self.execute_root([no, child, self.node("taken", 2)])
        await self.advance()
        self.assertEqual(self.calls, [1, 2])
        self.assertEqual(self.engine.tool_context[native.KEY]["states"]["also_skipped"], "skipped")

    async def test_missing_or_truncated_evidence_defers_without_guessing(self):
        node = self.node(); node["when"][0]["path"] = ["missing"]
        await self.execute_root([node]); await self.advance()
        self.assertEqual(self.calls, [1])
        self.assertEqual(self.engine.tool_context[native.KEY]["reason"], "guard_unknown")

    async def test_ambiguous_mutating_candidates_return_to_actor(self):
        await self.execute_root([self.node("a", 2), self.node("b", 3)])
        await self.advance()
        self.assertEqual(self.calls, [1])
        self.assertEqual(self.engine.tool_context[native.KEY]["reason"], "ambiguous_candidates")

    async def test_independent_safe_nodes_fan_out_and_join(self):
        active = []
        barrier = asyncio.Event()
        async def probe(value):
            self.calls.append(value)
            if value in (2, 3):
                active.append(value)
                if len(active) == 2: barrier.set()
                await asyncio.wait_for(barrier.wait(), 2)
            return {"value": value}
        handler = sequence.Probe(probe)
        handler.is_read_only = True
        handler.is_concurrency_safe_call = lambda **_: True
        self.engine.available_tools["sequence_probe"] = handler
        join = self.node("join", 4, ["a", "b"])
        await self.execute_root([self.node("a", 2), self.node("b", 3), join])
        await self.advance()
        self.assertEqual(self.calls, [1, 2, 3, 4])
        self.assertEqual(self.engine.tool_context[native.KEY]["status"], "exhausted")

    async def test_entire_graph_invalid_before_root_side_effect(self):
        bad = self.node(); bad["arguments"]["value"] = "2"
        receipt = await self.execute_root([bad])
        self.assertTrue(receipt["is_error"])
        self.assertEqual(self.calls, [])

    async def test_cancel_or_new_input_blocks_remaining_actions(self):
        await self.execute_root()
        self.engine.messages.append(create_user_message("Changed task"))
        await self.advance()
        self.assertEqual(self.calls, [1])
        self.assertEqual(self.engine.tool_context[native.KEY]["reason"], "new_input_or_scope_changed")

    async def test_parent_mailbox_consumed_before_model_fallback(self):
        pending = []
        self.engine.tool_context["delegated_mailbox"] = lambda: list(pending)
        self.engine.tool_context["ack_delegated_mailbox"] = lambda ids: pending.clear()
        async def probe(value):
            self.calls.append(value)
            pending.append({"id": "owned-input", "text": "Do not continue"})
            return {"value": value}
        self.engine.available_tools["sequence_probe"] = sequence.Probe(probe)
        self.fixture([("sequence_probe", json.dumps(self.root_call()["input"])), "NEW_INPUT_SEEN"])
        self.assertEqual(await self.run_task(), "NEW_INPUT_SEEN")
        self.assertEqual(self.calls, [1])
        self.assertIn("Do not continue", str(self.requests[1]["messages"]))

    async def test_approval_input_race_never_executes_stale_child(self):
        await self.execute_root()
        authorize = self.engine.permission_manager.authorize
        async def changed(*args, **kwargs):
            answer = await authorize(*args, **kwargs)
            self.engine.messages.append(create_user_message("Stop during approval"))
            return answer
        with patch.object(self.engine.permission_manager, "authorize", side_effect=changed):
            await self.advance()
        self.assertEqual(self.calls, [1])

    async def test_tool_replacement_blocks_old_plan(self):
        await self.execute_root()
        async def replacement(value):
            self.fail("Replaced tool must not execute")
        self.engine.available_tools["sequence_probe"] = sequence.Probe(replacement)
        await self.advance()
        self.assertEqual(self.calls, [1])
        self.assertEqual(self.engine.tool_context[native.KEY]["reason"], "tool_or_scope_changed")

    async def test_restore_reconciles_durable_result_without_replaying_root(self):
        await self.execute_root()
        saved = copy.deepcopy(self.engine.tool_context[native.KEY])
        native.restore(self.engine, saved)
        self.assertEqual(self.engine.tool_context[native.KEY]["status"], "active")
        await self.advance()
        self.assertEqual(self.calls, [1, 2])
        roots = [b for m in self.engine.messages for b in m.get("content", []) if isinstance(b, dict) and b.get("tool_use_id") == "owned-native-root"]
        self.assertEqual(len(roots), 1)

    async def test_uncertain_or_tampered_saved_graph_never_replays(self):
        await self.execute_root()
        saved = copy.deepcopy(self.engine.tool_context[native.KEY])
        saved["states"]["next"] = "running"
        native.restore(self.engine, saved)
        self.assertEqual(self.engine.tool_context[native.KEY]["reason"], "recovery_uncertain")
        saved["plan"]["nodes"][0]["arguments"]["value"] = 99
        native.restore(self.engine, saved)
        self.assertEqual(self.engine.tool_context[native.KEY]["reason"], "invalid_saved_continuation")
        await self.advance()
        self.assertEqual(self.calls, [1])

    async def test_save_failure_cannot_advance_an_uncommitted_result(self):
        original = self.engine.persist_session_state
        def save():
            if self.calls: return False
            return original()
        with patch.object(self.engine, "persist_session_state", side_effect=save):
            with self.assertRaises(PersistenceError):
                await self.execute_root()
        self.assertEqual(self.calls, [1])
        # Reload the authoritative pre-execution checkpoint, not in-memory success.
        saved = self.engine.session_manager.load_session_state(self.engine.session_id)
        native.restore(self.engine, saved["metadata"][native.KEY])
        await self.advance()
        self.assertEqual(self.calls, [1])

    async def test_cancel_before_boundary_does_not_execute(self):
        await self.execute_root()
        self.engine.abort_event.set()
        with self.assertRaises(asyncio.CancelledError):
            await self.advance()
        self.assertEqual(self.calls, [1])

    async def test_explicit_deny_remains_effective_for_predeclared_child(self):
        await self.execute_root()
        self.engine.permission_manager.always_deny_tools.add("sequence_probe")
        await self.advance()
        self.assertEqual(self.calls, [1])
        self.assertEqual(self.engine.tool_context[native.KEY]["reason"], "tool_failed")

    async def test_actual_checkpoint_load_then_explicit_resume_has_no_root_replay(self):
        await self.execute_root()
        identity = self.engine.session_id
        self.engine.tool_context.pop(native.KEY)
        self.assertTrue(self.engine.load_session(identity))
        self.assertEqual(self.engine.tool_context[native.KEY]["status"], "active")
        self.fixture(["RESUMED"])
        result = await self.engine._run_loop("", resume=True, sys_print_callback=lambda _: None)
        self.assertEqual(result.text, "RESUMED")
        self.assertEqual(self.calls, [1, 2])
        self.assertEqual(len(self.requests), 1)

    async def test_checkpoint_with_missing_result_evidence_cannot_resume_plan(self):
        await self.execute_root()
        saved = copy.deepcopy(self.engine.tool_context[native.KEY])
        self.engine.recent_tool_activity.clear()
        native.restore(self.engine, saved)
        self.assertEqual(self.engine.tool_context[native.KEY]["reason"], "invalid_saved_continuation")
        await self.advance()
        self.assertEqual(self.calls, [1])

    async def test_malformed_checkpoint_containers_defer_instead_of_breaking_history_load(self):
        await self.execute_root()
        saved = copy.deepcopy(self.engine.tool_context[native.KEY])
        for field in ("states", "contracts", "receipts", "observations", "certificates"):
            broken = copy.deepcopy(saved)
            broken[field] = ["root", "next"] if field != "certificates" else {}
            native.restore(self.engine, broken)
            self.assertEqual(self.engine.tool_context[native.KEY]["reason"], "invalid_saved_continuation")
            await self.advance()
        self.assertEqual(self.calls, [1])

    async def test_hook_cannot_rewrite_declared_root(self):
        from types import SimpleNamespace
        def changed(_):
            return SimpleNamespace(updated_input={"value": 99}, outputs=[], history=[], behavior="allow")
        with patch.object(self.engine.hook_manager, "evaluate_before_tool_hooks", side_effect=changed):
            receipt = await self.execute_root()
        self.assertTrue(receipt["is_error"])
        self.assertEqual(self.calls, [])

    async def test_new_process_recovers_pending_builtin_action_without_replaying_root(self):
        script = '''
import asyncio, json, sys
from pathlib import Path
from xuloom.core.engine import QueryEngine
from xuloom.core.messages import create_user_message, create_assistant_message
from xuloom.core.continuation import advance, FIELD, KEY
async def main():
    mode, folder, identity = sys.argv[1:]
    engine = QueryEngine(model="fixture", model_provider="openai", cwd=folder)
    engine.permission_manager.approval_policy.set_mode("full")
    try:
        if mode == "create":
            engine.permission_manager.approval_policy.set_mode("full")
            engine.messages.append(create_user_message("Owned process recovery"))
            block = {"type":"tool_use","id":"owned-process-root","name":"file_write_tool","input":{
                "file_path":str(Path(folder)/"root.txt"),"content":"ROOT",
                FIELD:{"nodes":[{"id":"next","tool":"file_write_tool","arguments":{
                    "file_path":str(Path(folder)/"next.txt"),"content":"NEXT"},
                    "when":[{"step":"root","path":["ok"],"equals":True}]}]}}}
            engine.messages.append(create_assistant_message([block]))
            receipt = await engine._execute_tool_wrapper(block, lambda _:None)
            assert not receipt["is_error"], receipt
        else:
            assert engine.load_session(identity)
            assert engine.tool_context[KEY]["status"] == "active", engine.tool_context[KEY]
            await advance(engine, turn=1, event_callback=None, sys_print_callback=lambda _:None)
            assert engine.tool_context[KEY]["status"] == "exhausted", engine.tool_context[KEY]
        print(json.dumps({"id":engine.session_id}))
    finally:
        await engine.tool_context["subagent_tasks"].close()
        await engine.tool_context["command_sessions"].close()
        await engine.client.close()
        await engine.compactor.client.close()
        engine.session_manager.release_writer()
asyncio.run(main())
'''
        folder = self.root / "owned-process"; folder.mkdir()
        env = {**os.environ, "OPENAI_API_KEY": "fixture"}
        command = [sys.executable, "-B", "-c", script]
        first = await asyncio.to_thread(subprocess.run, command + ["create", str(folder), ""], env=env,
            capture_output=True, text=True, timeout=45)
        self.assertEqual(first.returncode, 0, first.stderr)
        identity = json.loads(first.stdout.strip().splitlines()[-1])["id"]
        before = (folder / "root.txt").stat().st_mtime_ns
        second = await asyncio.to_thread(subprocess.run, command + ["recover", str(folder), identity], env=env,
            capture_output=True, text=True, timeout=45)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual((folder / "root.txt").stat().st_mtime_ns, before)
        self.assertEqual((folder / "next.txt").read_text(), "NEXT")
