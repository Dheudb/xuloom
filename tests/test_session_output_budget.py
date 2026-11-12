"""Lifetime token accounting must never impose an implicit conversation quota."""
import json
import unittest

import test_tool_recovery as recovery
from xuloom.core.engine_parts.loop import _CONTINUE
from xuloom.core.run_result import failure_help


class SessionOutputBudgetTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = recovery.RecoveryTests.asyncSetUp
    asyncTearDown = recovery.RecoveryTests.asyncTearDown
    fixture = recovery.RecoveryTests.fixture

    async def test_old_conversation_continues_without_resetting_usage_or_request_limits(self):
        self.engine.session_token_usage["output_tokens"] = 204217
        self.fixture(["RESUMED"])
        result = await self.engine.run("Continue the original task", sys_print_callback=lambda _: None)
        self.assertTrue(result.ok, result.text)
        self.assertEqual(result.text, "RESUMED")
        self.assertEqual(self.engine.session_token_usage["output_tokens"], 204222)
        self.assertEqual(self.requests[0]["max_tokens"], self.engine._get_max_output_tokens())

    async def test_restored_budget_stopped_checkpoint_can_start_another_round(self):
        self.engine.session_token_usage["output_tokens"] = 204217
        self.engine.last_run_result = {"reason": "budget_exhausted", "persisted": True}
        self.assertTrue(self.engine.persist_session_state())
        self.assertTrue(self.engine.load_session(self.engine.session_id))
        self.assertEqual(self.engine.session_token_usage["output_tokens"], 204217)
        self.fixture(["RESTORED"])
        result = await self.engine.run("Continue", sys_print_callback=lambda _: None)
        self.assertTrue(result.ok, result.text)
        self.assertEqual(result.text, "RESTORED")

    async def test_max_tokens_continuation_is_unlimited_by_default_and_json_safe(self):
        self.engine.session_token_usage["output_tokens"] = 1000000
        state = self.engine._build_loop_state()
        self.assertIsNone(state["session_output_budget"])
        events = []
        result = await self.engine._recover_max_tokens([], state, 1, lambda _: None, events.append)
        self.assertIs(result, _CONTINUE)
        self.assertIsNone(events[-1]["remaining_output_budget"])
        json.dumps(events, allow_nan=False)
        self.assertGreater(state["max_tokens"], 0)
        self.assertIn("Remaining output budget: unlimited", self.engine.get_runtime_summary())

    async def test_explicit_goal_budget_still_applies_and_unlimited_goal_stays_unlimited(self):
        goal = {"budget": {"output_tokens": 20}, "output_used": 7, "output_reserved": 3}
        self.engine.tool_context["goal_budget"] = goal
        self.engine.session_token_usage["output_tokens"] = 204217
        self.assertEqual(self.engine._remaining_output_budget(self.engine._build_loop_state()), 10)
        goal["budget"]["output_tokens"] = None
        self.assertIsNone(self.engine._remaining_output_budget(self.engine._build_loop_state()))

    async def test_explicit_budget_failure_has_specific_actionable_guidance(self):
        self.assertEqual(failure_help("budget_exhausted")["category"], "budget")
        self.assertIn("预算", failure_help("budget_exhausted")["message"])
