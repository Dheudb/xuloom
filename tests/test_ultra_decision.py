import asyncio
import copy
import json
import unittest
from unittest.mock import patch

from tests import test_tool_recovery as recovery
from xuloom.core.engine import QueryEngine
from xuloom.core.run_result import RunResult
from xuloom.agents.ultra_decision import recover, pending, DECISION_TOOL, schema


def answer(text="4"):
    return json.dumps({"decision":"answer", "answer":text})


def sequential():
    return json.dumps({'decision':'sequential','reason':'A short tightly coupled request needs no independent workers.',
        'main_task':'Handle the original request with the normal tools as needed.','workers':[]})


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

    async def test_simple_answer_runs_after_planning_without_workers_or_raw_json_in_display(self):
        self.fixture([sequential(), '4'])
        result = await self.run_task("2+2?")
        self.assertTrue(result.ok, result)
        self.assertEqual(result.text, "4")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual([t['function']['name'] for t in self.requests[0]['tools']], [DECISION_TOOL])
        self.assertEqual(self.requests[0]['tool_choice']['function']['name'], DECISION_TOOL)
        self.assertEqual(self.engine.subagent_registry, [])
        self.assertEqual(self.engine.tool_context["collaboration_run"]["decision"]["decision"], "sequential")
        self.assertFalse(any(e.get("type")=="text_delta" and '"decision"' in e.get("text", "") for e in self.events))
        self.assertEqual(self.recorder.record["pendingText"], "4", "Only the execution reply is presented")

    async def test_native_decision_tool_plans_without_dispatch_or_repair(self):
        self.fixture([(DECISION_TOOL, sequential()), 'NATIVE_REPLY'])
        with patch.object(self.engine, '_execute_tool_wrapper', side_effect=AssertionError('Decision is not an executable action')):
            result = await self.run_task("2+2?")
        self.assertTrue(result.ok, result)
        self.assertEqual(result.text, "NATIVE_REPLY")
        self.assertEqual(len(self.requests), 2)
        self.assertFalse(any(e['type'] in {'tool_protocol_error', 'tool_started', 'tool_scheduled'} for e in self.events))

    async def test_incomplete_native_answer_requires_real_plan_not_an_independent_fallback(self):
        incomplete = json.dumps({'decision':'answer', 'reason':'First research the available evidence before presenting ideas.'})
        self.fixture([(DECISION_TOOL, incomplete), (DECISION_TOOL, delegate(1)), ('record_tool', '{"value":"ACTUAL_WORK"}'), 'PROVISIONAL', 'VERIFIED_RESULT'])
        gate = asyncio.Event()
        async def child(*args, **kwargs):
            await gate.wait()
            return RunResult('INDEPENDENT_EVIDENCE', 'completed', True)
        async def observe(event):
            if event['type'] == 'subagents_waiting': gate.set()
        with patch.object(QueryEngine, 'run', child):
            result = await self.run_task('Investigate evidence, then answer', callback=observe)
        self.assertTrue(result.ok, result)
        self.assertEqual(result.text, 'VERIFIED_RESULT')
        self.assertEqual(self.tool.calls, ['ACTUAL_WORK'])
        self.assertEqual(len(self.requests), 5)
        self.assertEqual(sum(e['type'] == 'tool_protocol_error' for e in self.events), 1)
        self.assertEqual(len(self.engine.subagent_registry), 1)
        self.assertEqual(self.engine.tool_context['collaboration_run']['decision']['decision'], 'delegate')
        self.assertIn('INDEPENDENT_EVIDENCE', json.dumps(self.requests[-1]['messages']))

    async def test_incomplete_legacy_answer_can_search_in_normal_execution(self):
        from unittest.mock import AsyncMock
        from xuloom.tools.web_tool import WebSearchTool
        search = AsyncMock(return_value='Search evidence: https://example.invalid/fixture-paper')
        self.fixture([json.dumps({'decision':'answer', 'reason':'Need research first'}),
            sequential(),
            ('tool_search_tool', '{"query":"web search","activate":["web_search_tool"]}'),
            ('web_search_tool', '{"query":"closed-loop electrochemical process control","max_results":5}'), 'SOURCED_RESULT'])
        with patch.object(WebSearchTool, 'execute', search):
            result = await self.run_task('Search sources, then compare ideas')
        self.assertTrue(result.ok, result)
        self.assertEqual(result.text, 'SOURCED_RESULT')
        search.assert_awaited_once()
        self.assertEqual(search.await_args.kwargs['query'], 'closed-loop electrochemical process control')
        self.assertIn('fixture-paper', json.dumps(self.requests[-1]['messages']))
        self.assertEqual(sum(e['type'] == 'tool_protocol_error' for e in self.events), 1)

    async def test_empty_answer_with_work_assignments_or_invalid_fields_is_still_rejected(self):
        for payload in [dict(decision='answer', workers=[dict(description='Must not launch', prompt='Must not execute')]),
                dict(decision='answer', main_task='Conflicting assignment'), dict(decision='answer', extra='unknown')]:
            from types import SimpleNamespace
            from xuloom.agents.ultra_decision import convert
            from xuloom.core.messages import create_user_message
            from xuloom.protocols.dsml import ToolProtocolError
            response = SimpleNamespace(content=[self.engine._make_text_block(json.dumps(payload))], stop_reason='end_turn')
            with self.assertRaises(ToolProtocolError):
                convert(self.engine, response, create_user_message('Original task'))
        self.assertNotIn('decision', self.engine.tool_context.get('collaboration_run', {}))
        self.assertEqual(self.engine.subagent_registry, [])

    async def test_new_schema_has_no_answer_bypass_but_old_saved_decisions_still_restore(self):
        parameters = schema(self.engine)[0]['function']['parameters']
        self.assertEqual(set(parameters['required']), {'decision','reason','main_task','workers'})
        self.assertEqual(parameters['properties']['decision']['enum'], ['delegate','sequential'])
        self.assertNotIn('answer', parameters['properties'])
        from xuloom.core.messages import create_user_message, get_msg_uuid
        old_user = create_user_message('Old completed conversation')
        self.engine.tool_context['collaboration_run']['decision'] = {'decision':'answer','answer':'OLD',
            'user_message_id':get_msg_uuid(old_user),'calls':[]}
        with patch.object(self.engine, '_execute_tool_batches', side_effect=AssertionError('Old completed answer must not execute work')):
            await recover(self.engine, old_user)

    async def test_repeated_research_answer_without_any_plan_does_not_complete_or_fabricate_workers(self):
        self.fixture([answer('Ten unsupported research ideas')] * 3)
        result = await self.run_task('Research several independent scientific approaches')
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, 'tool_protocol_error')
        self.assertNotIn('Ten unsupported research ideas', result.text)
        self.assertEqual(self.engine.subagent_registry, [])
        self.assertFalse(any(e['type']=='text_delta' and 'unsupported research ideas' in e.get('text','') for e in self.events))

    async def test_native_decision_launches_workers_and_preserves_search_assignment(self):
        plan = json.loads(delegate(1))
        plan['workers'][0]['prompt'] = 'Search and verify distinct independent sources'
        self.fixture([(DECISION_TOOL, json.dumps(plan)), ("record_tool", '{"value":"SEARCH_EVIDENCE"}'), "WAIT", "SOURCED_SYNTHESIS"])
        tasks = []
        async def child(child, user_input, sys_print_callback):
            tasks.append(user_input)
            return RunResult("VERIFIED_SOURCE", "completed", True)
        with patch.object(QueryEngine, "run", child):
            result = await self.run_task("Search literature and compare alternatives")
        self.assertTrue(result.ok, result)
        self.assertEqual(tasks, ['Search and verify distinct independent sources'])
        self.assertEqual(self.tool.calls, ['SEARCH_EVIDENCE'])
        self.assertNotIn(DECISION_TOOL, [t['function']['name'] for t in self.requests[1]['tools']])
        self.assertNotIn('tool_choice', self.requests[1])
        self.assertFalse(any(e['type'] == 'tool_protocol_error' for e in self.events))

    async def test_native_malformed_and_multiple_decisions_never_dispatch(self):
        self.fixture([(DECISION_TOOL, '{"decision":"answer","decision":"delegate"}'), (DECISION_TOOL, sequential()), 'FIXED'])
        result = await self.run_task()
        self.assertTrue(result.ok, result)
        self.assertEqual(result.text, 'FIXED')
        self.assertEqual(self.tool.calls, [])
        self.assertEqual(self.engine.subagent_registry, [])

    async def test_anthropic_schema_declares_registered_roles_without_openai_wrapper(self):
        self.engine.model_provider = 'anthropic'
        try:
            tool, = schema(self.engine)
            self.assertEqual(tool['name'], DECISION_TOOL)
            self.assertIn('delegate', tool['input_schema']['properties']['decision']['enum'])
            self.assertIn('explore', tool['input_schema']['$defs']['Assignment']['properties']['subagent_type']['anyOf'][0]['enum'])
        finally:
            self.engine.model_provider = 'openai'

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
        self.fixture(["plain unstructured answer", sequential(), "REPAIRED"])
        result = await self.run_task()
        self.assertTrue(result.ok, result)
        self.assertEqual(result.text, "REPAIRED")
        self.assertEqual(len(self.requests), 3)
        self.assertIn("Ultra decision rejected", json.dumps(self.requests[1]["messages"]))
        self.assertEqual(self.engine.subagent_registry, [])

    async def test_repair_control_does_not_override_later_execution_or_new_human_input(self):
        self.fixture(['bad', json.dumps({'decision':'sequential', 'reason':'Need evidence', 'main_task':'Read evidence'}),
            ('record_tool', '{"value":"EVIDENCE"}'), 'DONE', sequential(), 'NEW_REPLY'])
        result = await self.run_task('Search and verify evidence')
        self.assertTrue(result.ok, result)
        self.assertIn('Ultra decision rejected', json.dumps(self.requests[1]['messages']))
        self.assertNotIn('Ultra decision rejected', json.dumps(self.requests[2]['messages']))
        self.assertIn('Ultra decision rejected', str(self.engine.messages), 'Diagnostic history is retained')
        result = await self.run_task('Hello in a new round')
        self.assertEqual(result.text, 'NEW_REPLY')
        self.assertNotIn('Ultra decision rejected', json.dumps(self.requests[-1]['messages']))

    async def test_unknown_role_invalidates_whole_plan_before_any_worker_starts(self):
        bad = json.loads(delegate()); bad["workers"][-1]["subagent_type"]="research-not-registered"
        self.fixture([json.dumps(bad), sequential(), "No execution"])
        result = await self.run_task()
        self.assertTrue(result.ok, result)
        self.assertEqual(self.engine.subagent_registry, [])
        self.assertIn("registered roles", json.dumps(self.requests[1]["messages"]))

    async def test_tools_attempted_before_decision_are_not_executed(self):
        self.fixture([("record_tool", '{"value":"MUST_NOT_RUN"}'), sequential(), "SAFE"])
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

