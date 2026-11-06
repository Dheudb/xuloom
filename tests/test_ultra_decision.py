import asyncio
import copy
import json
import unittest
from unittest.mock import patch

from tests import test_tool_recovery as recovery
from xuloom.core.engine import QueryEngine
from xuloom.core.run_result import RunResult
from xuloom.agents.ultra_decision import recover, pending


def answer(text="4"):
    return json.dumps({"decision":"answer", "answer":text})


def delegate(count=2):
    return json.dumps({"decision":"delegate", "reason":"Independent evidence checks", "main_task":"Main analysis and synthesis",
        "workers":[{"description":f"Evidence {i}", "prompt":f"Independently check evidence source {i}"} for i in range(count)]})


class UltraDecisionTests(unittest.IsolatedAsyncioTestCase):
    fixture = recovery.RecoveryTests.fixture
    asyncTearDown = recovery.RecoveryTests.asyncTearDown

    async def asyncSetUp(self):
        await recovery.RecoveryTests.asyncSetUp(self)
        self.engine.set_collaboration_mode("ultra")
        self.engine.permission_manager.approval_policy.set_mode("full")

    async def run_task(self, prompt="Handle the given task", callback=None, resume=False):
        async def emit(event):
            self.events.append(event); self.recorder.accept(event)
            if callback: await callback(event)
        return await self.engine._run_loop("" if resume else prompt, sys_print_callback=lambda _:None, event_callback=emit, resume=resume)

    async def test_simple_answer_is_one_request_no_workers_and_no_raw_json_in_display(self):
        self.fixture([answer()])
        result = await self.run_task("2+2?")
        self.assertTrue(result.ok, result)
        self.assertEqual(result.text, "4")
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("tools", self.requests[0])
        self.assertEqual(self.engine.subagent_registry, [])
        self.assertEqual(self.engine.tool_context["collaboration_run"]["decision"]["decision"], "answer")
        self.assertFalse(any(e.get("type")=="text_delta" and '"decision"' in e.get("text", "") for e in self.events))
        self.assertEqual(self.recorder.record["pendingText"], "", "The decision JSON is never presented as assistant text")

    async def test_sequential_requires_reason_and_then_uses_normal_tools(self):
        self.fixture([json.dumps({"decision":"sequential", "reason":"One tightly coupled local operation", "main_task":"Read and edit one item"}),
            ("record_tool", '{"value":"MAIN_WORK"}'), "DONE"])
        result = await self.run_task()
        self.assertTrue(result.ok, result)
        self.assertEqual(self.tool.calls, ["MAIN_WORK"])
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.engine.subagent_registry, [])
        self.assertIn("tools", self.requests[1])

    async def test_delegation_launches_different_branches_parent_works_and_waits_for_all(self):
        gate, started = asyncio.Event(), []
        async def child(child, user_input, sys_print_callback):
            started.append(user_input)
            self.assertEqual(child.collaboration_mode, "solo")
            await gate.wait()
            return RunResult("CHILD_EVIDENCE:"+user_input, "completed", True)
        async def observe(event):
            if event["type"]=="subagents_waiting" and event.get("count"):
                self.assertEqual(len(started), 2)
                self.assertEqual(self.tool.calls, ["PARENT_WORK"])
                gate.set()
        self.fixture([delegate(), ("record_tool", '{"value":"PARENT_WORK"}'), "PROVISIONAL", "SYNTHESIS"])
        with patch.object(QueryEngine, "run", child):
            result = await asyncio.wait_for(self.run_task(callback=observe), 15)
        self.assertTrue(result.ok, result)
        self.assertEqual(result.text, "SYNTHESIS")
        self.assertEqual(len(set(started)), 2)
        self.assertTrue(all(row["background"] and row["status"]=="completed" for row in self.engine.subagent_registry))
        self.assertIn("CHILD_EVIDENCE", json.dumps(self.requests[-1]["messages"]))
        self.assertEqual(len(self.requests), 4)

    async def test_one_meaningful_branch_is_not_padded_to_a_quota(self):
        self.fixture([delegate(1), "DONE", "DONE"])
        async def child(*args, **kwargs): return RunResult("EVIDENCE", "completed", True)
        with patch.object(QueryEngine, "run", child): result = await self.run_task()
        self.assertTrue(result.ok, result)
        self.assertEqual(len(self.engine.subagent_registry), 1)

    async def test_invalid_decision_is_repaired_before_any_execution(self):
        self.fixture(["plain unstructured answer", answer("REPAIRED")])
        result = await self.run_task()
        self.assertTrue(result.ok, result)
        self.assertEqual(result.text, "REPAIRED")
        self.assertEqual(len(self.requests), 2)
        self.assertIn("Ultra decision rejected", json.dumps(self.requests[1]["messages"]))
        self.assertEqual(self.engine.subagent_registry, [])

    async def test_unknown_role_invalidates_whole_plan_before_any_worker_starts(self):
        bad = json.loads(delegate()); bad["workers"][-1]["subagent_type"]="research-not-registered"
        self.fixture([json.dumps(bad), answer("No execution")])
        result = await self.run_task()
        self.assertTrue(result.ok, result)
        self.assertEqual(self.engine.subagent_registry, [])
        self.assertIn("registered roles", json.dumps(self.requests[1]["messages"]))

    async def test_tools_attempted_before_decision_are_not_executed(self):
        self.fixture([("record_tool", '{"value":"MUST_NOT_RUN"}'), answer("SAFE")])
        result = await self.run_task()
        self.assertTrue(result.ok, result)
        self.assertEqual(self.tool.calls, [])
        self.assertEqual(result.text, "SAFE")

    async def test_bad_decisions_exhaust_bounded_repairs_without_fake_completion(self):
        self.fixture(["bad"]*3)
        result = await self.run_task()
        self.assertFalse(result.ok)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(self.engine.subagent_registry, [])
        self.assertNotIn("ultra_deciding", self.engine.tool_context)

    async def test_explicit_denial_applies_to_planned_worker_launch(self):
        self.engine.permission_manager.always_deny_tools.add("agent_tool")
        self.fixture([delegate(1), "Worker delegation was denied; no work executed."])
        result = await self.run_task()
        self.assertEqual(self.engine.subagent_registry, [])
        self.assertIn("denied", result.text.lower())
        self.assertIn("permission_behavior: deny", json.dumps(self.requests[1]["messages"]))

    async def test_cancel_during_decision_keeps_no_accepted_plan_or_workers(self):
        self.fixture([answer()])
        async def cancel(event):
            if event["type"]=="stream_status": self.engine.abort_event.set()
        result = await self.run_task(callback=cancel)
        self.assertEqual(result.reason, "cancelled")
        self.assertNotIn("decision", self.engine.tool_context["collaboration_run"])
        self.assertEqual(self.engine.subagent_registry, [])
        self.assertNotIn("ultra_deciding", self.engine.tool_context)

    async def test_saved_plan_and_receipts_survive_reload_without_worker_replay(self):
        self.fixture([delegate(1), "DONE", "DONE"])
        async def child(*args, **kwargs): return RunResult("EVIDENCE", "completed", True)
        with patch.object(QueryEngine, "run", child): await self.run_task()
        decision = copy.deepcopy(self.engine.tool_context["collaboration_run"]["decision"])
        original = next(m for m in self.engine.messages if m.get("_uuid")==decision["user_message_id"])
        before = len(self.engine.subagent_registry)
        with patch.object(self.engine, "_execute_tool_batches", side_effect=AssertionError("Already paired workers must not replay")):
            await recover(self.engine, original)
        self.engine.session_manager.release_writer()
        restored = QueryEngine(model="fixture", model_provider="openai", cwd=str(self.root))
        try:
            self.assertTrue(restored.load_session(self.engine.session_id))
            self.assertFalse(pending(restored))
            self.assertEqual(restored.tool_context["collaboration_run"]["decision"], decision)
            self.assertEqual(len(restored.subagent_registry), before)
        finally: await restored.aclose()

    async def test_checkpoint_failure_prevents_planned_launch(self):
        self.fixture([delegate(1)])
        save = self.engine.persist_session_state
        def fail_after_decision():
            if self.engine.tool_context.get("collaboration_run", {}).get("decision"): return False
            return save()
        with patch.object(self.engine, "persist_session_state", side_effect=fail_after_decision):
            result = await self.run_task()
        self.assertEqual(result.reason, "persistence_error")
        self.assertEqual(self.engine.subagent_registry, [])

    async def test_missing_launch_receipt_reuses_native_worker_identity(self):
        self.fixture([delegate(1), "DONE", "DONE"])
        async def child(*args, **kwargs): return RunResult("EVIDENCE", "completed", True)
        with patch.object(QueryEngine, "run", child): await self.run_task()
        decision = self.engine.tool_context["collaboration_run"]["decision"]
        decision.pop("receipts_saved")
        identity = decision["calls"][0]["id"]
        for message in self.engine.messages:
            if isinstance(message.get("content"), list):
                message["content"] = [block for block in message["content"] if block.get("tool_use_id") != identity]
        original = next(m for m in self.engine.messages if m.get("_uuid") == decision["user_message_id"])
        before = len(self.engine.subagent_registry)
        with patch.object(QueryEngine, "run", side_effect=AssertionError("Never relaunch existing worker")):
            await recover(self.engine, original)
        self.assertEqual(len(self.engine.subagent_registry), before)
        self.assertTrue(decision["receipts_saved"])

    async def test_corrupted_saved_launch_arguments_are_rejected_before_execution(self):
        self.fixture([delegate(1), "DONE", "DONE"])
        async def child(*args, **kwargs): return RunResult("EVIDENCE", "completed", True)
        with patch.object(QueryEngine, "run", child): await self.run_task()
        decision = self.engine.tool_context["collaboration_run"]["decision"]
        decision["calls"][0]["input"]["prompt"] = "Different task"
        original = next(m for m in self.engine.messages if m.get("_uuid") == decision["user_message_id"])
        with patch.object(self.engine, "_execute_tool_batches", side_effect=AssertionError("No invalid recovery execution")):
            with self.assertRaisesRegex(ValueError, "do not match"): await recover(self.engine, original)

