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

    async def test_simple_reply_uses_one_native_assessment_request_without_workers(self):
        self.fixture([('ultra_answer','{"answer":"4"}')])
        result = await self.run_task('2+2?')
        self.assertEqual(result.text, '4'); self.assertTrue(result.ok)
        self.assertEqual(len(self.requests), 1)
        request = self.requests[0]
        self.assertEqual(request['tool_choice'],'required')
        names = {t['function']['name'] for t in request['tools']}
        self.assertEqual(names,{'ultra_answer','ultra_prepare'})
        self.assertNotIn('collaboration_decision', names)
        self.assertNotIn('Ultra collaboration decision', json.dumps(request['messages']))
        self.assertNotIn('decision', self.engine.tool_context['collaboration_run'])
        self.assertEqual(self.engine.subagent_registry, [])
        self.assertFalse(any(e['type']=='collaboration_decision' for e in self.events))

    async def test_assessment_search_runs_automatically_without_another_model_instruction(self):
        from xuloom.tools.web_tool import WebSearchTool
        search = AsyncMock(return_value='Inspected evidence: https://example.invalid/fixture-paper')
        self.fixture([('ultra_prepare', json.dumps(dict(task_type='research',main_task='Compare evidence',evidence='search',
            search_queries=['compare independent approaches'],rationale='External evidence is needed'))), 'SOURCED_RESULT'])
        with patch.object(WebSearchTool, 'execute', search): result = await self.run_task('Research and compare alternatives')
        self.assertTrue(result.ok, result); search.assert_awaited_once()
        self.assertEqual(len(self.requests),2)
        self.assertIn('Inspected evidence',json.dumps(self.requests[-1]['messages']))
        system = '\n'.join(m['content'] for m in self.requests[0]['messages'] if m['role']=='system')
        self.assertIn('use ultra_prepare for research',system)
        self.assertNotIn('Scientific/technical ideation',system)

    async def test_sequential_work_is_not_forced_to_launch_workers(self):
        self.fixture([('ultra_prepare',json.dumps(dict(task_type='sequential',main_task='Record work',evidence='not_needed',rationale='One dependent operation'))),
            ('record_tool','{"value":"WORK"}'),'DONE'])
        result=await self.run_task()
        self.assertTrue(result.ok,result); self.assertEqual(self.tool.calls,['WORK'])
        self.assertEqual(len(self.requests),3)

    async def test_native_workers_and_parent_work_run_before_wait_all_synthesis(self):
        gate=asyncio.Event();started=[];events=[]
        async def child(child,user_input,sys_print_callback):
            started.append(user_input);self.assertEqual(child.collaboration_mode,'solo')
            await gate.wait();return RunResult('EVIDENCE:'+user_input,'completed',True)
        async def observe(event):
            events.append(event)
            if event['type']=='subagents_waiting' and event.get('count'):
                self.assertEqual(set(started),{'Branch A','Branch B'})
                self.assertEqual(self.tool.calls,['PARENT_WORK']);gate.set()
        self.fixture([('ultra_prepare',json.dumps(dict(task_type='multi_path',main_task='Do main work',evidence='provided',rationale='Independent branches',
            workers=[{'prompt':'Branch A','description':'A'},{'prompt':'Branch B','description':'B'}]))),
            ('record_tool','{"value":"PARENT_WORK"}'),'PROVISIONAL','SYNTHESIS'])
        with patch.object(QueryEngine,'run',child): result=await asyncio.wait_for(self.run_task(callback=observe),15)
        self.assertTrue(result.ok,result);self.assertEqual(result.text,'SYNTHESIS')
        self.assertEqual(len(self.requests),4)
        self.assertEqual(len(self.engine.subagent_registry),2)
        self.assertTrue(all(r['background'] and r['status']=='completed' for r in self.engine.subagent_registry))
        self.assertIn('EVIDENCE:Branch A',json.dumps(self.requests[-1]['messages']))
        self.assertIn('EVIDENCE:Branch B',json.dumps(self.requests[-1]['messages']))
        drafts = [e for e in events if e['type']=='text_delta' and e.get('text')=='PROVISIONAL']
        self.assertTrue(drafts)
        self.assertTrue(all(e.get('channel')=='commentary' for e in drafts))
        self.assertTrue(all(e.get('channel')!='commentary' for e in events if e['type']=='text_delta' and e.get('text')=='SYNTHESIS'))

    async def test_one_worker_is_not_padded_to_a_quota(self):
        self.fixture([('ultra_prepare',json.dumps(dict(task_type='multi_path',main_task='Synthesize',evidence='provided',rationale='One useful check',
            workers=[{'prompt':'One independent check','description':'Check'}]))),'DONE','DONE'])
        async def child(*args,**kwargs):return RunResult('EVIDENCE','completed',True)
        with patch.object(QueryEngine,'run',child):result=await self.run_task()
        self.assertTrue(result.ok,result);self.assertEqual(len(self.engine.subagent_registry),1)

    async def test_denied_native_spawn_is_not_executed(self):
        self.engine.permission_manager.always_deny_tools.add('agent_tool')
        self.fixture([('ultra_prepare',json.dumps(dict(task_type='multi_path',main_task='Synthesize',evidence='provided',rationale='One check',
            workers=[{'prompt':'Independent check','description':'Check'}]))),'Delegation denied.'])
        result=await self.run_task();self.assertTrue(result.ok,result)
        self.assertEqual(self.engine.subagent_registry,[])
        self.assertIn('permission_behavior: deny',json.dumps(self.requests[1]['messages']))

    async def test_missing_assessment_is_repaired_instead_of_becoming_a_final_answer(self):
        self.fixture(['PREMATURE_ANSWER',('ultra_answer','{"answer":"VALID_SIMPLE_ANSWER"}')])
        result=await self.run_task('Hello')
        self.assertTrue(result.ok,result)
        self.assertEqual(result.text,'VALID_SIMPLE_ANSWER')
        self.assertEqual(len(self.requests),2)
        self.assertTrue(any(e['type']=='tool_protocol_error' for e in self.events))
        self.assertNotEqual(self.recorder.record.get('answer'),'PREMATURE_ANSWER')

    async def test_undeclared_initial_work_cannot_bypass_assessment_or_create_side_effects(self):
        self.fixture([('record_tool','{"value":"MUST_NOT_EXECUTE"}'),('ultra_answer','{"answer":"A simple reply"}')])
        result=await self.run_task('Hello')
        self.assertTrue(result.ok,result)
        self.assertEqual(self.tool.calls,[])
        self.assertIn('not executed',json.dumps(self.requests[-1]['messages']))

    async def test_anthropic_assessment_keeps_thinking_and_uses_supported_auto_tool_choice(self):
        import importlib
        from anthropic import AsyncAnthropic,DefaultAsyncHttpxClient
        httpx=importlib.import_module('httpx2' if any(c.__module__.startswith('httpx2') for c in DefaultAsyncHttpxClient.__mro__) else 'httpx')
        await self.engine.client.close()
        self.engine.model_provider='anthropic';self.engine.api_base_url='https://api.anthropic.com'
        self.engine.reasoning_profile={'adapter':'anthropic-budget','efforts':[],'canDisable':True,
            'thinking':'enabled','effort':'default','budgetTokens':2048}
        requests=[]
        def handler(request):
            body=json.loads(request.content);requests.append(body)
            message={'id':'owned-message','type':'message','role':'assistant','model':'fixture','content':[],
                'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':10,'output_tokens':0}}
            events=[{'type':'message_start','message':message},
                {'type':'content_block_start','index':0,'content_block':{'type':'tool_use','id':'owned-call','name':'ultra_answer','input':{}}},
                {'type':'content_block_delta','index':0,'delta':{'type':'input_json_delta','partial_json':'{"answer":"ANTHROPIC_SIMPLE"}'}},
                {'type':'content_block_stop','index':0},
                {'type':'message_delta','delta':{'stop_reason':'tool_use','stop_sequence':None},'usage':{'output_tokens':12}},
                {'type':'message_stop'}]
            return httpx.Response(200,headers={'content-type':'text/event-stream'},text=''.join('event: '+e['type']+'\ndata: '+json.dumps(e)+'\n\n' for e in events))
        self.engine.client=AsyncAnthropic(api_key='owned-fixture',http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler),trust_env=False))
        result=await self.run_task('Hello')
        self.assertTrue(result.ok,result)
        self.assertEqual(result.text,'ANTHROPIC_SIMPLE')
        self.assertEqual(len(requests),1)
        self.assertEqual(requests[0]['thinking'],{'type':'enabled','budget_tokens':2048})
        self.assertEqual(requests[0]['tool_choice']['type'],'auto')

    async def test_invalid_research_plan_repairs_before_actions_and_dispatches_one_distinct_worker(self):
        from xuloom.tools.web_tool import WebSearchTool
        plan=dict(task_type='multi_path',main_task='Synthesize approaches',evidence='search',search_queries=['owned query','owned query'],
            workers=[{'prompt':'Independent evidence','description':'Check'},{'prompt':'Independent evidence','description':'Check'}],rationale='Independent evidence and main synthesis')
        invalid={**plan,'workers':[]}
        self.fixture([('ultra_prepare',json.dumps(invalid)),('ultra_prepare',json.dumps(plan)),'FINAL','FINAL'])
        search=AsyncMock(return_value='OWNED_SEARCH_EVIDENCE')
        async def child(*args,**kwargs):return RunResult('WORKER_EVIDENCE','completed',True)
        with patch.object(WebSearchTool,'execute',search),patch.object(QueryEngine,'run',child):result=await self.run_task()
        self.assertTrue(result.ok,result)
        search.assert_awaited_once()
        self.assertEqual(len(self.engine.subagent_registry),1,'Identical assignments are not multiplied')
        self.assertIn('OWNED_SEARCH_EVIDENCE',json.dumps(self.requests[-1]['messages']))

    async def test_same_prompt_keeps_distinct_roles_and_explicit_replicate_labels(self):
        plan=dict(task_type='multi_path',main_task='Compare independent results',evidence='provided',rationale='Different roles and explicit repeat labels',
            workers=[{'prompt':'Check the supplied evidence','description':'Check','subagent_type':'general-purpose'},
                {'prompt':'Check the supplied evidence','description':'Check','subagent_type':'explore'},
                {'prompt':'Check the supplied evidence','description':'Explicit repeat 2','subagent_type':'general-purpose'}])
        self.fixture([('ultra_prepare',json.dumps(plan)),'FINAL','FINAL'])
        async def child(*args,**kwargs):return RunResult('INDEPENDENT_RESULT','completed',True)
        with patch.object(QueryEngine,'run',child):result=await self.run_task()
        self.assertTrue(result.ok,result)
        self.assertEqual(len(self.engine.subagent_registry),3)

    async def test_resuming_after_accepted_plan_does_not_repeat_search(self):
        from xuloom.tools.web_tool import WebSearchTool
        plan=dict(task_type='research',main_task='Synthesize',evidence='search',search_queries=['owned query'],rationale='Evidence needed')
        self.fixture([('ultra_prepare',json.dumps(plan))])
        search=AsyncMock(return_value='PERSISTED_EVIDENCE')
        original=self.engine.persist_session_state
        def stop_after_receipt():
            saved=original()
            if any(row.get('tool_name',row.get('name'))=='ultra_prepare' for row in self.engine.recent_tool_activity): self.engine.abort_event.set()
            return saved
        with patch.object(WebSearchTool,'execute',search):
            with patch.object(self.engine,'persist_session_state',side_effect=stop_after_receipt):first=await self.run_task()
            self.assertEqual(first.reason,'cancelled',first)
            self.requests=[];self.fixture(['RESUMED_SYNTHESIS'])
            self.engine.abort_event.clear()
            result=await self.run_task(resume=True)
        self.assertTrue(result.ok,result)
        self.assertEqual(result.text,'RESUMED_SYNTHESIS')
        search.assert_awaited_once()

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
