import asyncio
import json
import sys
import unittest
from unittest.mock import patch

from pydantic import BaseModel, ConfigDict
from xuloom.core.messages import create_user_message
from xuloom.tools.base import BaseAgenticTool
from tests import test_tool_recovery as recovery


class ProbeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    value: int


class Probe(BaseAgenticTool):
    name = "sequence_probe"
    description = "Owned deterministic receipt probe"
    input_schema = ProbeInput
    def __init__(self, execute):
        self.callback = execute
    async def execute(self, value):
        return await self.callback(value)


class ToolSequenceTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = recovery.RecoveryTests.asyncTearDown
    fixture = recovery.RecoveryTests.fixture
    run_task = recovery.RecoveryTests.run_task
    async def asyncSetUp(self):
        await recovery.RecoveryTests.asyncSetUp(self)
        self.engine.activate_tools(["tool_sequence"])
        self.calls = []
        async def probe(value):
            self.calls.append(value)
            return {"value": value, "ready": True}
        self.engine.available_tools["sequence_probe"] = Probe(probe)
        self.engine.permission_manager.approval_policy.set_mode("full")
        self.engine.messages.append(create_user_message("Complete the owned sequence"))
        self.engine._sync_message_context()

    def steps(self):
        return [{"id": "first", "tool": "sequence_probe", "arguments": {"value": 1}},
                {"id": "second", "tool": "sequence_probe", "arguments": {"value": 2},
                 "when": [{"step": "first", "path": ["value"], "equals": 1}]}]

    async def call(self, steps=None, identity="owned-sequence"):
        captured = []
        receipt = await self.engine._execute_tool_wrapper({"id": identity, "name": "tool_sequence", "input": {"steps": self.steps() if steps is None else steps}},
            lambda _: None, result_callback=captured.append)
        return receipt, json.loads(captured[0]["content"]) if captured else None

    async def test_literal_actions_use_native_journal_and_repeated_call_is_not_executed_twice(self):
        receipt, result = await self.call()
        self.assertFalse(receipt["is_error"], receipt)
        self.assertEqual(self.calls, [1, 2])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["steps"]), 2)
        self.assertEqual(len([r for r in self.engine.recent_tool_activity if r["tool_name"] == "sequence_probe"]), 2)
        await self.call()
        self.assertEqual(self.calls, [1, 2])

    async def test_guard_requires_exact_types_and_complete_json(self):
        for expected, path in ((True, ["value"]), (1, ["missing"]), (2, ["value"])):
            steps = self.steps(); steps[1]["when"][0].update(equals=expected, path=path)
            _, result = await self.call(steps, identity="guard-" + str(len(self.calls)))
            self.assertEqual(result["reason"], "guard_not_proven")
        self.assertEqual(self.calls, [1, 1, 1])

    async def test_entire_plan_validates_before_any_side_effect(self):
        for bad in ({"id": "first", "tool": "sequence_probe", "arguments": {"value": 2}},
                    {"id": "invalid", "tool": "tool_sequence", "arguments": {}},
                    {"id": "invalid", "tool": "sequence_probe", "arguments": {"value": "2"}},
                    {"id": "invalid", "tool": "sequence_probe", "arguments": {"value": 2}, "when": [{"step": "future", "path": ["x"], "equals": 1}]}):
            receipt, _ = await self.call([self.steps()[0], bad], identity="invalid-" + str(bad))
            self.assertTrue(receipt["is_error"])
        self.assertEqual(self.calls, [])

    async def test_nested_permission_denial_and_plan_mode_are_enforced(self):
        self.engine.permission_manager.always_deny_tools.add("sequence_probe")
        receipt, result = await self.call()
        self.assertTrue(receipt["is_error"])
        self.assertIn("tool_failed", receipt["content"])
        self.assertEqual(self.calls, [])
        self.engine.permission_manager.always_deny_tools.clear()
        self.engine.set_mode("plan")
        receipt, _ = await self.call(identity="plan")
        self.assertTrue(receipt["is_error"])
        self.assertEqual(self.calls, [])

    async def test_new_parent_message_or_scope_change_defers_remaining_steps(self):
        async def probe(value):
            self.calls.append(value)
            self.engine.tool_context["delegated_mailbox"] = lambda: [{"id": "new-message", "text": "Stop this plan"}]
            return {"value": value}
        self.engine.available_tools["sequence_probe"] = Probe(probe)
        _, result = await self.call()
        self.assertEqual(result["reason"], "new_input_or_scope_changed")
        self.assertEqual(result["pending"], ["second"])
        self.assertEqual(self.calls, [1])
        self.engine.tool_context.pop("delegated_mailbox")
        async def changed(value):
            self.calls.append(value)
            self.engine._api_key = "different-owned-key"
            return {"value": value}
        self.engine.available_tools["sequence_probe"] = Probe(changed)
        _, result = await self.call(identity="changed-scope")
        self.assertEqual(result["reason"], "new_input_or_scope_changed")
        self.assertEqual(self.calls, [1, 1])

    async def test_cancellation_prevents_next_action(self):
        async def probe(value):
            self.calls.append(value); self.engine.abort_event.set()
            return {"value": value}
        self.engine.available_tools["sequence_probe"] = Probe(probe)
        with self.assertRaises(asyncio.CancelledError):
            await self.call()
        self.assertEqual(self.calls, [1])

    async def test_real_failed_command_does_not_advance(self):
        steps = [{"id": "command", "tool": "command_tool", "arguments": {"argv": [sys.executable, "-c", "raise SystemExit(7)"], "wait_ms": 10000}}, self.steps()[0]]
        receipt, _ = await self.call(steps)
        self.assertTrue(receipt["is_error"])
        self.assertIn('"reason": "tool_failed"', receipt["content"])
        self.assertEqual(self.calls, [])

    async def test_hook_cannot_change_a_literal_action(self):
        from types import SimpleNamespace
        original = self.engine.hook_manager.evaluate_before_tool_hooks
        def changed(payload):
            if payload["tool_name"] != "sequence_probe": return original(payload)
            return SimpleNamespace(updated_input={"value": 99}, outputs=[], history=[], behavior="allow")
        with patch.object(self.engine.hook_manager, "evaluate_before_tool_hooks", side_effect=changed):
            receipt, result = await self.call()
        self.assertTrue(receipt["is_error"])
        self.assertEqual(self.calls, [])

    async def test_native_model_loop_executes_two_steps_with_one_tool_response(self):
        self.fixture([("tool_sequence", json.dumps({"steps": self.steps()})), "DONE"])
        self.assertEqual(await self.run_task(), "DONE")
        self.assertEqual(self.calls, [1, 2])
        self.assertEqual(len(self.requests), 2)
        self.assertIn('"step": "second"', json.dumps(self.requests[1]["messages"]).replace('\\"', '"'))
        self.assertEqual(self.engine.session_token_usage["input_tokens"], 20)
        self.assertEqual(self.engine.session_token_usage["output_tokens"], 10)

    async def test_parent_input_is_consumed_by_model_before_any_further_action(self):
        pending = []
        self.engine.tool_context["delegated_mailbox"] = lambda: list(pending)
        self.engine.tool_context["ack_delegated_mailbox"] = lambda ids: pending.clear()
        async def probe(value):
            self.calls.append(value)
            pending.append({"id": "owned-message", "text": "Stop the previous plan"})
            return {"value": value}
        self.engine.available_tools["sequence_probe"] = Probe(probe)
        self.fixture([("tool_sequence", json.dumps({"steps": self.steps()})), "NEW_INPUT_HANDLED"])
        self.assertEqual(await self.run_task(), "NEW_INPUT_HANDLED")
        self.assertEqual(self.calls, [1])
        self.assertIn("Stop the previous plan", str(self.requests[1]["messages"]))
        self.assertEqual(pending, [])

    async def test_result_persistence_failure_never_advances_or_replays(self):
        from xuloom.core.run_result import PersistenceError
        original = self.engine.persist_session_state
        def save():
            if any(row["tool_name"] == "sequence_probe" for row in self.engine.recent_tool_activity):
                return False
            return original()
        with patch.object(self.engine, "persist_session_state", side_effect=save):
            with self.assertRaises(PersistenceError):
                await self.call()
        self.assertEqual(self.calls, [1])
        receipt, _ = await self.call()
        self.assertTrue(receipt["is_error"])
        self.assertEqual(self.calls, [1])

    async def test_new_message_during_approval_prevents_the_action_itself(self):
        self.engine.permission_manager.approval_policy.set_mode("ask")
        async def approve(request):
            self.engine.tool_context["delegated_mailbox"] = lambda: [{"id": "in-flight", "text": "New instructions"}]
            return "allow"
        self.engine.permission_manager.prompt_handler = approve
        receipt, _ = await self.call()
        self.assertTrue(receipt["is_error"])
        self.assertIn("sequence_scope_changed", receipt["content"])
        self.assertEqual(self.calls, [])

    async def test_ambiguous_truncated_and_partial_receipts_never_activate_guards(self):
        from xuloom.tools.tool_sequence import guard_matches, ReceiptGuard
        guard = ReceiptGuard(step="source", path=["status"], equals="completed")
        for value in ('{"status":"failed","status":"completed"}', {"status": "completed", "metadata": {"truncated": True}}, "status: completed"):
            self.assertFalse(guard_matches(guard, {"source": value}))
        output = ReceiptGuard(step="source", path=["output", "text"], equals="OK")
        self.assertFalse(guard_matches(output, {"source": {"output": {"text": "OK", "eof": False, "has_more": True}}}))

    async def test_isolated_file_steps_use_confined_backend_and_never_the_host_handler(self):
        from unittest.mock import AsyncMock
        self.engine.execution_sandbox.mode = "workspace"
        guarded = AsyncMock(return_value={"ok": True, "is_error": False, "content": "owned confined result", "metadata": {}})
        host = AsyncMock(side_effect=AssertionError("Isolated writes must never execute on the host"))
        steps = [{"id": "first", "tool": "file_write_tool", "arguments": {"file_path": str(self.root / "must-not-write.txt"), "content": "owned"}}]
        with patch.object(self.engine.execution_sandbox, "tool", guarded), patch.object(self.engine.available_tools["file_write_tool"], "execute", host):
            receipt, result = await self.call(steps)
        self.assertFalse(receipt["is_error"], receipt)
        self.assertEqual(result["status"], "completed")
        guarded.assert_awaited_once()
        host.assert_not_awaited()
        self.assertFalse((self.root / "must-not-write.txt").exists())

    async def test_each_child_requests_its_own_approval_without_wrapper_approval(self):
        from unittest.mock import AsyncMock
        self.engine.permission_manager.approval_policy.set_mode("ask")
        handler = AsyncMock(return_value="allow")
        self.engine.permission_manager.prompt_handler = handler
        receipt, _ = await self.call()
        self.assertFalse(receipt["is_error"], receipt)
        self.assertEqual([item.args[0].tool_name for item in handler.await_args_list], ["sequence_probe", "sequence_probe"])

    async def test_cross_process_goal_pause_is_checked_between_steps(self):
        from xuloom.features.goals import request_control
        self.engine.goal = {"id": "owned-goal", "status": "active"}
        async def probe(value):
            self.calls.append(value)
            request_control(self.engine.session_cwd, self.engine.session_id, "owned-goal", "pause")
            return {"value": value}
        self.engine.available_tools["sequence_probe"] = Probe(probe)
        with self.assertRaises(asyncio.CancelledError):
            await self.call()
        self.assertEqual(self.calls, [1])
        self.assertEqual(self.engine.goal["status"], "paused")

    async def test_replaced_tool_cannot_receive_an_old_planned_action(self):
        async def replacement(value):
            raise AssertionError("A changed executor must not receive the old action")
        async def first(value):
            self.calls.append(value)
            self.engine.available_tools["sequence_probe"] = Probe(replacement)
            return {"value": value}
        self.engine.available_tools["sequence_probe"] = Probe(first)
        _, result = await self.call()
        self.assertEqual(result["reason"], "tool_contract_changed")
        self.assertEqual(self.calls, [1])
