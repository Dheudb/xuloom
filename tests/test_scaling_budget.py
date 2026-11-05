import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from xuloom.scaling import budget
from xuloom.scaling.control import Controller
from xuloom.scaling.store import Store


class BudgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'budget.sqlite3')
        self.run={'id':'run','token':'t','status':'running','target':10,'budget':{'output_tokens':100},'nodes':[]}
        self.store.put('run',self.run)
        self.node={'id':'host','check':{'quota':'quota'}}
        self.replies=[]
        self.controller=Controller(self.store,local=object())
        async def remote(node,method,params): self.replies.append((method,params));return True
        self.controller.remote=remote

    def request(self,key,maximum=80):
        return {'id':key,'task_id':'task','kind':'model_permit','status':'pending','payload':{
            'maximum':maximum,'quota':'quota','limits':{'concurrency':8,'requests_per_minute':None}}}

    async def test_concurrent_requests_share_one_cap_and_replay_does_not_charge_twice(self):
        a,b=self.request('a'),self.request('b')
        await asyncio.gather(self.controller.requests(self.run,self.node,[a]),self.controller.requests(self.run,self.node,[b]))
        self.assertEqual([self.store.get('permit',k)['output_reserved'] for k in ['a','b']],[80,20])
        await self.controller.requests(self.run,self.node,[a])
        self.assertEqual(budget.view(self.store,self.run)['committed_output_tokens'],100)
        self.assertEqual(self.replies[-1][1]['answer']['maximum'],80)

    async def test_unknown_usage_is_charged_and_ack_replay_retains_evidence(self):
        a=self.request('a');await self.controller.requests(self.run,self.node,[a])
        a['status']='released'
        await self.controller.requests(self.run,self.node,[a]);await self.controller.requests(self.run,self.node,[a])
        view=budget.view(self.store,self.run)
        self.assertEqual(view['committed_output_tokens'],80);self.assertEqual(view['unknown_usage_requests'],1)
        a['reported']=1
        await self.controller.requests(self.run,self.node,[a])
        self.assertEqual(budget.view(self.store,self.run)['committed_output_tokens'],80)

    async def test_confirmed_usage_refunds_unused_reservation_only_once(self):
        a=self.request('a');await self.controller.requests(self.run,self.node,[a])
        await self.controller.requests(self.run,self.node,[dict(a,status='released',reported=30)])
        b=self.request('b');await self.controller.requests(self.run,self.node,[b])
        self.assertEqual(self.store.get('permit','b')['output_reserved'],70)
        self.assertEqual(budget.view(self.store,self.run)['reported_output_tokens'],30)

    async def test_exhaustion_rejects_before_inference_and_cannot_resume_by_clearing_usage(self):
        await self.controller.requests(self.run,self.node,[self.request('a',100)])
        await self.controller.requests(self.run,self.node,[self.request('b')])
        self.assertFalse(self.replies[-1][1]['answer']['ok'])
        self.assertTrue(self.store.get('run','run')['budget_stopped'])
        with self.assertRaisesRegex(ValueError,'Budget admission'):
            await self.controller.dispatch('resume',{'id':'run'})

    async def test_deadline_rejects_admission_even_with_tokens_remaining(self):
        self.run['budget']['deadline']=time.time()-1
        await self.controller.requests(self.run,self.node,[self.request('a')])
        self.assertFalse(self.replies[-1][1]['answer']['ok'])

    def test_offline_envelopes_are_durable_and_never_refunded_on_disconnect(self):
        a={'id':'leader-a','count':5};b={'id':'leader-b','count':5}
        first=budget.allocate_child(self.controller,self.run,a)
        second=budget.allocate_child(self.controller,self.run,b)
        self.assertEqual((first['output_tokens'],second['output_tokens']),(40,40))
        restarted=Controller(Store(self.store.path),local=object())
        self.assertEqual(budget.allocate_child(restarted,self.run,a),first)
        with self.store.connection() as db:self.assertEqual(budget.grant(db,self.run,100),20)
        self.assertEqual(budget.view(self.store,self.run)['offline_reserved_output'],80)

    def test_invalid_or_unknown_budget_values_fail_closed(self):
        for value in [{'dollars':1},{'output_tokens':True},{'seconds':0},{'output_tokens':1.5},[]]:
            with self.assertRaises(ValueError):budget.normalize(value)
        self.assertEqual(budget.normalize(None),{})

    def test_foreign_settlement_cannot_release_credit(self):
        self.store.put('permit',{'id':'a','run_id':'other','quota':'quota','status':'granted','output_reserved':50})
        with self.assertRaises(ValueError):budget.settle(self.store,self.run,{'id':'a'},'quota')
        self.assertEqual(self.store.get('permit','a')['status'],'granted')

    async def test_provider_overreport_remains_visible_and_stops_future_credit(self):
        a=self.request('a');await self.controller.requests(self.run,self.node,[a])
        await self.controller.requests(self.run,self.node,[dict(a,status='released',reported=120)])
        self.assertEqual(budget.view(self.store,self.run)['committed_output_tokens'],120)
        with self.store.connection() as db:self.assertEqual(budget.grant(db,self.run,80),0)

    async def test_compaction_uses_the_same_admission_and_usage_receipt(self):
        from xuloom.memory.memory import MemoryCompactor
        compactor=MemoryCompactor.__new__(MemoryCompactor)
        compactor.model='fixture';compactor.provider='openai';compactor.client=object()
        compactor.summary_max_tokens=50;compactor.summary_window=100000
        compactor.estimate_tokens=lambda _:1
        settled=[]
        async def acquire(maximum):return {'id':'compact','maximum':min(12,maximum)}
        compactor.budget_engine=type('Engine',(),{'tool_context':{'acquire_model_budget':acquire,
            'settle_model_budget':lambda lease,reported:settled.append((lease,reported))}})()
        async def fake(client,model,provider,messages,maximum,**kwargs):
            self.assertEqual(maximum,12);kwargs['usage_callback']('compaction',{'output_tokens':7});return 'summary'
        with patch('xuloom.core.config.safe_llm_call',fake):
            self.assertEqual(await compactor.compact_history([{'role':'user','content':'evidence'}]),'summary')
        self.assertEqual(settled[0][1],7)
