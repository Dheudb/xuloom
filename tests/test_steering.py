import asyncio
import json
import uuid
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch
from tests import test_tool_recovery as recovery
from xuloom.core.messages import create_user_message
from xuloom.core.run_result import PersistenceError
from xuloom.core.steering import withdraw
from xuloom.core.engine import QueryEngine


class SteeringTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = recovery.RecoveryTests.asyncSetUp
    asyncTearDown = recovery.RecoveryTests.asyncTearDown

    async def execute(self, text='ORIGINAL', **kwargs):
        async for event in self.engine.submit_message(text,sys_print_callback=lambda _:None,**kwargs):
            self.events.append(event)
        return self.events[-1]['result']

    def held_model(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def create(**request):
            self.requests.append(request);number=len(self.requests)
            async def chunks():
                if number==1:
                    entered.set();await release.wait()
                content='OLD' if number==1 else 'NEW_DIRECTION_DONE'
                yield NS(choices=[NS(delta=NS(content=content,tool_calls=[]),finish_reason='stop')],usage=NS(prompt_tokens=10,completion_tokens=5))
            return chunks()
        self.engine.client.chat.completions.create=create
        return entered,release

    async def test_stream_input_is_durable_fifo_idempotent_and_cannot_finish_early(self):
        entered,release=self.held_model();task=asyncio.create_task(self.execute());await entered.wait()
        identity=str(uuid.uuid4());first=self.engine.queue_input('CHANGE A',identity)
        self.assertEqual(self.engine.queue_input('CHANGE A',identity),first)
        with self.assertRaises(ValueError):self.engine.queue_input('DIFFERENT',identity)
        self.engine.queue_input('CHANGE B')
        raw=self.engine.session_manager.load_session_state(self.engine.session_id)
        self.assertEqual(len(raw['metadata']['user_inbox']),2)
        release.set();result=await task
        self.assertTrue(result['ok']);self.assertEqual(len(self.requests),2)
        human=[m['content'] for m in self.requests[-1]['messages'] if m['role']=='user']
        self.assertIn('CHANGE A',str(human));self.assertIn('CHANGE B',str(human))
        self.assertEqual(self.engine.user_inbox,[])
        delivered=[e for e in self.events if e['type']=='user_input_applied']
        self.assertEqual([i['text'] for i in delivered[0]['inputs']],['CHANGE A','CHANGE B'])
        self.assertEqual(self.engine.queue_input('CHANGE A',identity)['status'],'applied')

    async def test_running_tool_finishes_once_but_old_unstarted_call_is_deferred(self):
        entered,release=asyncio.Event(),asyncio.Event();self.engine.MAX_TOOL_CONCURRENCY=1
        async def tool(value=''):
            self.tool.calls.append(value);entered.set();await release.wait();return 'saved:'+value
        self.tool.execute=tool
        async def create(**request):
            self.requests.append(request);number=len(self.requests)
            async def chunks():
                calls=[NS(index=i,id='old-'+str(i),function=NS(name='record_tool',arguments=json.dumps({'value':v}))) for i,v in enumerate(['KEEP','SKIP'])]
                yield NS(choices=[NS(delta=NS(content=None if number==1 else 'ADJUSTED',tool_calls=calls if number==1 else []),finish_reason='tool_calls' if number==1 else 'stop')],usage=None)
            return chunks()
        self.engine.client.chat.completions.create=create
        task=asyncio.create_task(self.execute());await asyncio.wait_for(entered.wait(),5)
        self.engine.queue_input('Keep existing work; change the remaining plan');release.set()
        result=await task;self.assertTrue(result['ok']);self.assertEqual(self.tool.calls,['KEEP'])
        text=json.dumps(self.requests[-1]['messages']);self.assertIn('saved:KEEP',text);self.assertIn('Not executed: new user instructions',text)

    async def test_stop_and_reload_keep_undelivered_input_for_explicit_resume(self):
        entered,_=self.held_model();task=asyncio.create_task(self.execute());await entered.wait()
        self.engine.queue_input('AFTER_RESTART');await self.engine.request_abort();await task
        identity=self.engine.session_id;await self.engine.aclose()
        self.engine=QueryEngine(model='fixture',model_provider='openai',cwd=str(self.root))
        self.assertTrue(self.engine.load_session(identity));self.assertEqual(self.engine.user_inbox[0]['text'],'AFTER_RESTART')
        requests=[]
        async def create(**request):
            requests.append(request)
            async def chunks():yield NS(choices=[NS(delta=NS(content='RESUMED',tool_calls=[]),finish_reason='stop')],usage=None)
            return chunks()
        self.engine.client.chat.completions.create=create
        result=await self.execute('',resume=True)
        self.assertTrue(result['ok']);self.assertIn('AFTER_RESTART',json.dumps(requests[0]['messages']));self.assertEqual(self.engine.user_inbox,[])

    async def test_failed_admission_is_not_acknowledged_or_delivered(self):
        entered,_=self.held_model();task=asyncio.create_task(self.execute());await entered.wait()
        with patch.object(self.engine,'persist_session_state',return_value=False):
            with self.assertRaises(PersistenceError):self.engine.queue_input('UNSAVED')
        self.assertEqual(self.engine.user_inbox,[]);await self.engine.request_abort();await task
        self.assertEqual(len(self.requests),1)

    async def test_withdraw_does_not_apply_input_or_add_model_requests(self):
        entered,release=self.held_model();task=asyncio.create_task(self.execute());await entered.wait()
        receipt=self.engine.queue_input('WITHDRAW');self.assertTrue(withdraw(self.engine,receipt['id']))
        self.assertFalse(withdraw(self.engine,receipt['id']));release.set();result=await task
        self.assertTrue(result['ok']);self.assertEqual(len(self.requests),1);self.assertFalse(self.engine.user_input_event.is_set())

    async def test_failed_delivery_keeps_saved_input_and_never_starts_next_request(self):
        entered,release=self.held_model();task=asyncio.create_task(self.execute());await entered.wait()
        self.engine.queue_input('SAVED_BUT_NOT_DELIVERED')
        real_save=self.engine.persist_session_state
        def save():
            if not self.engine.user_inbox and any(m.get('_steering_id') for m in self.engine.messages): return False
            return real_save()
        with patch.object(self.engine,'persist_session_state',side_effect=save):
            release.set();result=await task
        self.assertEqual(result['reason'],'persistence_error');self.assertFalse(result['ok'])
        self.assertEqual(len(self.requests),1)
        self.assertEqual(self.engine.user_inbox[0]['text'],'SAVED_BUT_NOT_DELIVERED')
        raw=self.engine.session_manager.load_session_state(self.engine.session_id)
        self.assertEqual(raw['metadata']['user_inbox'][0]['text'],'SAVED_BUT_NOT_DELIVERED')

    async def test_paused_goal_resumes_saved_input_without_replacing_its_objective(self):
        from xuloom.features import goals
        self.engine.require_writer()
        goals.create(self.engine,'ORIGINAL_GOAL',['ORIGINAL_GOAL'],rounds=3)
        entered,_=self.held_model()
        async def emit(event): self.events.append(event)
        task=asyncio.create_task(goals.run(self.engine,emit));await entered.wait()
        self.engine.queue_input('EXTRA_GOAL_REQUIREMENT')
        await goals.control(self.engine,'pause','owned test pause');await task
        with self.assertRaises(ValueError):goals.update_objective(self.engine,'OTHER_GOAL')
        await goals.run(self.engine,emit)
        self.assertEqual(self.engine.user_inbox,[])
        self.assertEqual(self.engine.goal['objective'],'ORIGINAL_GOAL')
        self.assertIn('EXTRA_GOAL_REQUIREMENT',json.dumps(self.requests[-1]['messages']))

    async def test_pending_input_prevents_handler_after_permission_wait(self):
        entered,release=asyncio.Event(),asyncio.Event()
        self.engine.require_writer();self.engine.messages.append(create_user_message('TASK'))
        self.engine.active_run_task=asyncio.current_task()
        async def authorize(*_,**kwargs):entered.set();await release.wait();return NS(behavior='allow')
        with patch.object(self.engine.permission_manager,'authorize',side_effect=authorize):
            task=asyncio.create_task(self.engine._execute_tool_wrapper({'type':'tool_use','id':'p','name':'record_tool','input':{'value':'NO'}},lambda _:None))
            await entered.wait();self.engine.queue_input('CHANGED');release.set();receipt=await task
        self.engine.active_run_task=None
        self.assertTrue(receipt['is_error']);self.assertEqual(self.tool.calls,[])

    async def test_owned_worker_wait_yields_for_input_without_cancelling_workers(self):
        self.engine.require_writer();self.engine.active_run_task=asyncio.current_task()
        release=asyncio.Event();worker=asyncio.create_task(release.wait())
        self.engine.tool_context['subagent_tasks'].tasks['owned']=worker
        waiter=asyncio.create_task(self.engine._collect_subagents_before_final(turn=1))
        await asyncio.sleep(.02);self.engine.queue_input('REVIEW DIRECTION')
        self.assertTrue(await asyncio.wait_for(waiter,.5));self.assertFalse(worker.done())
        release.set();await worker;self.engine.active_run_task=None
