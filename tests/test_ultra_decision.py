"""Native Ultra execution and compatibility with previously saved routing journals."""
import asyncio
import copy
import json
import unittest
from unittest.mock import AsyncMock, patch
from tests import test_tool_recovery as recovery
from xuloom.core.engine import QueryEngine
from xuloom.core.run_result import RunResult
from xuloom.core.messages import create_user_message, create_assistant_message, get_msg_uuid
from xuloom.agents.ultra_decision import Decision, calls_for, recover, request_messages


class UltraDecisionTests(unittest.IsolatedAsyncioTestCase):
    fixture = recovery.RecoveryTests.fixture
    asyncTearDown = recovery.RecoveryTests.asyncTearDown

    async def asyncSetUp(self):
        await recovery.RecoveryTests.asyncSetUp(self)
        self.engine.set_collaboration_mode('ultra')
        self.engine.permission_manager.approval_policy.set_mode('full')

    async def run_task(self, prompt='Handle the given task', callback=None, resume=False):
        async def emit(event):
            self.events.append(event); self.recorder.accept(event)
            if callback: await callback(event)
        return await self.engine._run_loop('' if resume else prompt, sys_print_callback=lambda _:None, event_callback=emit, resume=resume)

    async def test_simple_reply_uses_one_normal_request_without_a_routing_model_or_workers(self):
        self.fixture(['4'])
        result = await self.run_task('2+2?')
        self.assertEqual(result.text, '4'); self.assertTrue(result.ok)
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertNotIn('tool_choice', request)
        names = {t['function']['name'] for t in request['tools']}
        self.assertTrue({'spawn_agent','web_search_tool','web_fetch_tool'} <= names)
        self.assertNotIn('collaboration_decision', names)
        self.assertNotIn('Ultra collaboration decision', json.dumps(request['messages']))
        self.assertNotIn('decision', self.engine.tool_context['collaboration_run'])
        self.assertEqual(self.engine.subagent_registry, [])
        self.assertFalse(any(e['type']=='collaboration_decision' for e in self.events))

    async def test_search_is_callable_from_first_request_without_a_discovery_or_routing_round(self):
        from xuloom.tools.web_tool import WebSearchTool
        search = AsyncMock(return_value='Inspected evidence: https://example.invalid/fixture-paper')
        self.fixture([('web_search_tool', '{"query":"compare independent approaches","max_results":5}'), 'SOURCED_RESULT'])
        with patch.object(WebSearchTool, 'execute', search): result = await self.run_task('Research and compare alternatives')
        self.assertTrue(result.ok, result); search.assert_awaited_once()
        self.assertEqual(len(self.requests),2)
        self.assertIn('Inspected evidence',json.dumps(self.requests[-1]['messages']))
        system = '\n'.join(m['content'] for m in self.requests[0]['messages'] if m['role']=='system')
        self.assertIn('Use subagents when parallel work would materially improve speed or quality.',system)
        self.assertNotIn('Scientific/technical ideation',system)

    async def test_normal_tool_action_is_not_blocked_by_a_preliminary_decision(self):
        self.fixture([('record_tool','{"value":"WORK"}'),'DONE'])
        result=await self.run_task()
        self.assertTrue(result.ok,result); self.assertEqual(self.tool.calls,['WORK'])
        self.assertEqual(len(self.requests),2)

    async def test_native_workers_and_parent_work_run_before_wait_all_synthesis(self):
        gate=asyncio.Event();started=[]
        async def child(child,user_input,sys_print_callback):
            started.append(user_input);self.assertEqual(child.collaboration_mode,'solo')
            await gate.wait();return RunResult('EVIDENCE:'+user_input,'completed',True)
        async def observe(event):
            if event['type']=='subagents_waiting' and event.get('count'):
                self.assertEqual(set(started),{'Branch A','Branch B'})
                self.assertEqual(self.tool.calls,['PARENT_WORK']);gate.set()
        self.fixture([('spawn_agent','{"prompt":"Branch A","description":"A"}'),
            ('spawn_agent','{"prompt":"Branch B","description":"B"}'),
            ('record_tool','{"value":"PARENT_WORK"}'),'PROVISIONAL','SYNTHESIS'])
        with patch.object(QueryEngine,'run',child): result=await asyncio.wait_for(self.run_task(callback=observe),15)
        self.assertTrue(result.ok,result);self.assertEqual(result.text,'SYNTHESIS')
        self.assertEqual(len(self.requests),5)
        self.assertEqual(len(self.engine.subagent_registry),2)
        self.assertTrue(all(r['background'] and r['status']=='completed' for r in self.engine.subagent_registry))
        self.assertIn('EVIDENCE:Branch A',json.dumps(self.requests[-1]['messages']))
        self.assertIn('EVIDENCE:Branch B',json.dumps(self.requests[-1]['messages']))

    async def test_one_worker_is_not_padded_to_a_quota(self):
        self.fixture([('spawn_agent','{"prompt":"One independent check"}'),'DONE','DONE'])
        async def child(*args,**kwargs):return RunResult('EVIDENCE','completed',True)
        with patch.object(QueryEngine,'run',child):result=await self.run_task()
        self.assertTrue(result.ok,result);self.assertEqual(len(self.engine.subagent_registry),1)

    async def test_denied_native_spawn_is_not_executed(self):
        self.engine.permission_manager.always_deny_tools.add('agent_tool')
        self.fixture([('spawn_agent','{"prompt":"Independent check"}'),'Delegation denied.'])
        result=await self.run_task();self.assertTrue(result.ok,result)
        self.assertEqual(self.engine.subagent_registry,[])
        self.assertIn('permission_behavior: deny',json.dumps(self.requests[1]['messages']))

    def seed_legacy(self):
        self.engine.require_writer()
        user=create_user_message('Legacy task')
        plan=Decision(decision='delegate',reason='Independent check',main_task='Main task',
            workers=[{'prompt':'Legacy evidence','description':'Legacy check'}])
        calls=calls_for(plan,get_msg_uuid(user))
        decision={'user_message_id':get_msg_uuid(user),**plan.model_dump(),'calls':calls}
        self.engine.tool_context['collaboration_run']['decision']=decision
        content=[self.engine._make_text_block('Collaboration decision: Independent check')]+[
            self.engine._make_tool_use_block(tool_id=c['id'],name=c['name'],input_payload=c['input']) for c in calls]
        self.engine.messages=[user,create_assistant_message([self.engine._block_to_dict(block) for block in content])]
        self.engine._sync_message_context();self.assertTrue(self.engine.persist_session_state())
        return user,decision

    async def test_legacy_missing_receipt_reuses_native_worker_identity(self):
        user,decision=self.seed_legacy();finished=asyncio.Event()
        async def child(*args,**kwargs):finished.set();return RunResult('LEGACY_EVIDENCE','completed',True)
        with patch.object(QueryEngine,'run',child):
            await recover(self.engine,user);await asyncio.wait_for(finished.wait(),10)
        before=len(self.engine.subagent_registry);self.assertEqual(before,1)
        decision.pop('receipts_saved')
        identity=decision['calls'][0]['id']
        for message in self.engine.messages:
            if isinstance(message.get('content'),list):
                message['content']=[b for b in message['content'] if b.get('tool_use_id')!=identity]
        with patch.object(QueryEngine,'run',side_effect=AssertionError('Never relaunch an existing worker')):
            await recover(self.engine,user)
        self.assertEqual(len(self.engine.subagent_registry),before)
        self.assertTrue(decision['receipts_saved'])

    async def test_legacy_completed_plan_survives_reload_without_replay(self):
        user,decision=self.seed_legacy();decision['receipts_saved']=True
        self.assertTrue(self.engine.persist_session_state());sid=self.engine.session_id
        self.engine.session_manager.release_writer()
        restored=QueryEngine(model='fixture',model_provider='openai',cwd=str(self.root))
        try:
            self.assertTrue(restored.load_session(sid))
            with patch.object(restored,'_execute_tool_batches',side_effect=AssertionError('No replay')):
                await recover(restored,user)
            self.assertEqual(restored.tool_context['collaboration_run']['decision'],decision)
        finally:await restored.aclose()

    async def test_corrupted_or_foreign_legacy_plan_is_rejected_before_execution(self):
        user,decision=self.seed_legacy()
        with patch.object(self.engine,'_execute_tool_batches',side_effect=AssertionError('No invalid execution')):
            with self.assertRaisesRegex(ValueError,'different task'):await recover(self.engine,create_user_message('Other'))
            decision['calls'][0]['input']['prompt']='Changed'
            with self.assertRaisesRegex(ValueError,'do not match'):await recover(self.engine,user)

    async def test_old_completed_answer_requires_no_execution(self):
        user=create_user_message('Old completed answer')
        self.engine.tool_context['collaboration_run']['decision']={'user_message_id':get_msg_uuid(user),
            'decision':'answer','answer':'OLD','calls':[]}
        with patch.object(self.engine,'_execute_tool_batches',side_effect=AssertionError('No replay')):
            await recover(self.engine,user)

    def test_obsolete_routing_advice_is_omitted_from_requests_without_rewriting_history(self):
        messages=[{'role':'user','content':'Collaboration decision: user supplied quotation'},
            {'role':'user','_is_meta':True,'_origin':'tool_protocol','content':'[Ultra decision rejected before any tool execution] retry classifier'},
            {'role':'assistant','_is_meta':True,'content':[{'type':'text','text':'Collaboration decision: I can answer from memory'}]},
            {'role':'assistant','content':[{'type':'text','text':'Collaboration decision: old delegated plan'},
                {'type':'tool_use','id':'old','name':'spawn_agent','input':{}}]}]
        original=copy.deepcopy(messages);projected=request_messages(self.engine,messages)
        self.assertEqual(messages,original);self.assertEqual(projected,[messages[0],messages[3]])
