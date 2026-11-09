"""Official defaults, account isolation and the actual Responses SDK wire, offline."""
import copy
import asyncio
import json
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from openai import AsyncOpenAI
from xuloom.core.model_specs import official, with_defaults, OPENAI_MODELS
from xuloom.core.model_catalog import public_config, select_reasoning
from xuloom.context.compaction import policy
from xuloom.core.config import safe_llm_call
import test_tool_recovery as recovery

BASE = 'https://api.openai.com/v1'


class OfficialModelSpecsTests(unittest.TestCase):
    def test_official_models_have_separate_context_output_and_exact_efforts(self):
        for name, disable in OPENAI_MODELS.items():
            spec = official('openai', name, BASE)
            self.assertEqual((spec['window'], spec['maxOutput']), (1050000, 128000))
            self.assertEqual('none' in spec['efforts'], disable)
            self.assertNotIn('minimal', spec['efforts'])
        for base in ('http://api.openai.com/v1', 'https://gateway.invalid/v1', 'https://api.openai.com.evil/v1', 'https://api.openai.com/v1?route=other'):
            self.assertIsNone(official('openai', 'gpt-6.1-sol', base))
        self.assertIsNone(official('openai', 'gpt-6.1-sol-custom', BASE))
        self.assertIsNone(official('anthropic', 'gpt-6.1-sol', BASE))

    def test_all_models_are_exposed_with_defaults_without_leaking_secret(self):
        cfg = {'model_connections': [{'id': 'official', 'name': 'Official', 'provider': 'openai', 'base_url': BASE, 'api_key': 'owned-secret', 'models': list(OPENAI_MODELS), 'model': 'gpt-6.1-sol'}]}
        original = copy.deepcopy(cfg)
        result = public_config(cfg)
        self.assertEqual(cfg, original)
        for name in OPENAI_MODELS:
            self.assertEqual(result['modelContext'][name]['window'], 1050000)
            self.assertEqual(result['modelInputs'][name], ['text', 'image'])
            self.assertEqual(result['modelReasoning'][name]['effort'], 'medium')
        self.assertNotIn('owned-secret', json.dumps(result))
        updated = select_reasoning(cfg, {'connectionId': 'official', 'model': 'gpt-6.1-sol', 'effort': 'max'})
        self.assertEqual(public_config(updated)['modelReasoning']['gpt-6.1-sol']['effort'], 'max')
        with self.assertRaises(ValueError):
            select_reasoning(cfg, {'connectionId': 'official', 'model': 'gpt-6.1-sol', 'effort': 'none'})

    def test_explicit_overrides_win_and_unknown_routes_stay_conservative(self):
        item = {'provider': 'openai', 'models': ['gpt-6-sol'], 'base_url': BASE, 'model_context': {'gpt-6-sol': {'window': 128000}}, 'model_reasoning': {'gpt-6-sol': {'adapter': 'openai', 'efforts': ['low'], 'effort': 'low'}}}
        resolved = with_defaults(item)
        self.assertEqual(resolved['model_context']['gpt-6-sol']['window'], 128000)
        self.assertEqual(resolved['model_reasoning'], item['model_reasoning'])
        engine = NS(primary_model='gpt-6-sol', model_provider='openai', api_base_url=BASE, _model_contract={'context': {}})
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual(policy(engine)['window'], 1050000)
            engine.api_base_url = 'https://gateway.invalid/v1'
            self.assertEqual(policy(engine)['window'], 65536)
            engine._model_contract = {'context': {'window': 200000}}
            self.assertEqual(policy(engine)['window'], 200000)


class OfficialResponsesWireTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = recovery.RecoveryTests.asyncSetUp
    asyncTearDown = recovery.RecoveryTests.asyncTearDown

    async def test_luna_high_reasoning_reports_actual_activity_without_exposing_opaque_content(self):
        await self.engine.client.close()
        self.engine.primary_model = self.engine.model = 'gpt-6-luna'
        self.engine.api_base_url = BASE
        self.engine.reasoning_profile = with_defaults({'provider':'openai', 'models':['gpt-6-luna'], 'base_url':BASE})['model_reasoning']['gpt-6-luna']
        self.engine.reasoning_profile['effort'] = 'high'
        self.engine.set_collaboration_mode('auto')
        requests, events = [], []
        def handler(request):
            body = json.loads(request.content); requests.append(body)
            opaque = {'type':'reasoning', 'id':'rs_owned', 'summary':[], 'encrypted_content':'OPAQUE_CANARY'}
            answer = {'type':'message', 'id':'msg_owned', 'role':'assistant', 'status':'completed',
                'content':[{'type':'output_text', 'text':'LOCAL_ANSWER', 'annotations':[]}]}
            response = {'id':'resp_owned', 'object':'response', 'status':'completed', 'model':body['model'],
                'output':[opaque, answer], 'usage':{'input_tokens':20, 'output_tokens':15, 'total_tokens':35,
                    'output_tokens_details':{'reasoning_tokens':10}}}
            stream = [{'type':'response.output_item.added', 'output_index':0, 'item':opaque},
                {'type':'response.output_item.done', 'output_index':0, 'item':opaque},
                {'type':'response.output_text.delta', 'delta':'LOCAL_ANSWER'},
                {'type':'response.output_item.done', 'output_index':1, 'item':answer},
                {'type':'response.completed', 'response':response}]
            data = ''.join('event: '+event['type']+'\ndata: '+json.dumps(event)+'\n\n' for event in stream)
            return httpx.Response(200, headers={'content-type':'text/event-stream'}, text=data)
        async def emit(event):
            events.append(event); self.recorder.accept(event)
        self.engine.client = AsyncOpenAI(api_key='owned-fixture', base_url=BASE,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False))
        result = await self.engine._run_loop('A fixture question', sys_print_callback=lambda _:None, event_callback=emit)
        self.assertTrue(result.ok, result)
        self.assertEqual(len(requests),1)
        self.assertEqual(requests[0]['reasoning'], {'effort':'high'})
        self.assertIn('file_read_tool',{t['name'] for t in requests[0]['tools']})
        self.assertIn('# Collaboration: auto', requests[0]['instructions'])
        self.assertNotIn('max_tokens', requests[0])
        metrics = self.recorder.record['modelStreams'][0]
        self.assertEqual(metrics['status'],'completed')
        self.assertEqual(metrics['reasoningTokens'],10)
        self.assertEqual(metrics['reasoningItems'],1)
        self.assertEqual(metrics['toolChunks'],0)
        self.assertTrue(any(e['type']=='stream_status' and e['metrics']['status']=='reasoning' for e in events))
        self.assertNotIn('OPAQUE_CANARY', json.dumps(self.recorder.record))
        self.assertNotIn('OPAQUE_CANARY', json.dumps(events))
        self.assertEqual(self.engine.messages[-1]['_responses_output'][0]['encrypted_content'],'OPAQUE_CANARY')

    async def test_native_stream_timeout_retains_partial_text_and_error_status(self):
        from xuloom.context.responses import receive
        from xuloom.protocols.stream_watch import StreamInterrupted
        class Stream:
            closed = False
            async def __aiter__(self):
                yield {'type':'response.output_text.delta','delta':'PARTIAL'}
                await asyncio.sleep(10)
            async def close(self): self.closed = True
        stream = Stream()
        async def emit(event): self.recorder.accept(event)
        with patch.object(self.engine.client.responses,'create',AsyncMock(return_value=stream)), patch('xuloom.protocols.stream_watch.stream_limits',return_value=(.1,.02,.01)):
            with self.assertRaises(StreamInterrupted) as failure:
                await receive(self.engine,system_prompt='Fixture',api_messages=[],tools_schema=[],turn_count=1,
                    model_name='fixture',max_tokens=100,event_callback=emit)
        self.assertEqual(failure.exception.partial_text,'PARTIAL')
        self.assertEqual(self.recorder.record['modelStreams'][0]['status'],'error')
        self.assertTrue(stream.closed)
        self.assertIsNone(self.engine.active_stream)

    async def test_native_user_abort_has_cancelled_status_and_normal_abort_contract(self):
        from xuloom.context.responses import receive
        from xuloom.core.model_stream import AbortRequestedError
        class Stream:
            closed = False
            async def __aiter__(self):
                yield {'type':'response.output_item.added','item':{'type':'reasoning','summary':[]}}
                await asyncio.sleep(10)
            async def close(self): self.closed = True
        stream = Stream()
        async def emit(event):
            self.recorder.accept(event)
            if event['type']=='stream_status' and event['metrics']['status']=='reasoning': self.engine.abort_event.set()
        with patch.object(self.engine.client.responses,'create',AsyncMock(return_value=stream)):
            with self.assertRaises(AbortRequestedError):
                await receive(self.engine,system_prompt='Fixture',api_messages=[],tools_schema=[],turn_count=1,
                    model_name='fixture',max_tokens=100,event_callback=emit)
        self.assertEqual(self.recorder.record['modelStreams'][0]['status'],'cancelled')
        self.assertTrue(stream.closed)
        self.assertIsNone(self.engine.active_stream)

    async def test_ultra_simple_answer_uses_native_responses_tool_choice_in_one_request(self):
        await self.engine.client.close()
        self.engine.primary_model = self.engine.model = 'gpt-6.1-sol'
        self.engine.api_base_url = BASE
        self.engine.set_collaboration_mode('ultra')
        requests = []
        def handler(request):
            self.assertEqual(str(request.url), BASE + '/responses')
            body = json.loads(request.content); requests.append(body)
            self.assertEqual(body['tool_choice'],'required')
            names = {tool['name'] for tool in body['tools']}
            self.assertEqual(names,{'ultra_answer','ultra_prepare'})
            self.assertNotIn('collaboration_decision',names)
            item = {'type':'function_call','id':'simple_call','call_id':'simple_call','status':'completed',
                'name':'ultra_answer','arguments':json.dumps({'answer':'NATIVE_ULTRA'})}
            response = {'id': 'resp_decision', 'object': 'response', 'status': 'completed', 'model': body['model'],
                'output': [item], 'usage': {'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15}}
            data = 'event: response.completed\ndata: ' + json.dumps({'type': 'response.completed', 'response': response}) + '\n\n'
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, text=data)
        self.engine.client = AsyncOpenAI(api_key='owned-fixture', base_url=BASE,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False))
        result = await self.engine._run_loop('Hello', sys_print_callback=lambda _: None)
        self.assertTrue(result.ok, result)
        self.assertEqual(result.text, 'NATIVE_ULTRA')
        self.assertEqual(len(requests), 1)
        self.assertEqual(self.tool.calls, [])
        self.assertEqual(self.engine.subagent_registry, [])

    async def test_real_sdk_uses_responses_and_keeps_reasoning_tool_identity(self):
        await self.engine.client.close()
        self.engine.primary_model = self.engine.model = 'gpt-6.1-sol'
        self.engine.api_base_url = BASE
        self.engine.reasoning_profile = with_defaults({'provider': 'openai', 'models': ['gpt-6.1-sol'], 'base_url': BASE})['model_reasoning']['gpt-6.1-sol']
        self.engine.reasoning_profile['effort'] = 'high'
        self.engine.permission_manager.approval_policy.set_mode('full')
        requests = []
        def handler(request):
            self.assertEqual(str(request.url), BASE + '/responses')
            body = json.loads(request.content); requests.append(body)
            outputs = [item for item in body['input'] if item.get('type') == 'function_call_output']
            if outputs:
                self.assertEqual(outputs[0]['call_id'], 'owned_call')
                self.assertIn('saved:after', outputs[0]['output'])
                self.assertTrue(any(item.get('encrypted_content') == 'opaque-owned' for item in body['input']))
                items = [{'type': 'message', 'id': 'msg_owned', 'role': 'assistant', 'status': 'completed', 'phase': 'final_answer', 'content': [{'type': 'output_text', 'text': 'DONE', 'annotations': []}]}]
            else:
                items = [{'type': 'reasoning', 'id': 'rs_owned', 'summary': [], 'encrypted_content': 'opaque-owned'}, {'type': 'function_call', 'id': 'fc_owned', 'call_id': 'owned_call', 'name': 'record_tool', 'arguments': '{"value":"after"}', 'status': 'completed'}]
            response = {'id': 'resp_owned', 'object': 'response', 'status': 'completed', 'model': body['model'], 'output': items, 'usage': {'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15}}
            events = ([{'type': 'response.output_text.delta', 'delta': 'DONE'}] if outputs else []) + [{'type': 'response.completed', 'response': response}]
            data = ''.join('event: '+event['type']+'\ndata: '+json.dumps(event)+'\n\n' for event in events)
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, text=data)
        self.engine.client = AsyncOpenAI(api_key='owned-fixture', base_url=BASE, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False))
        result = await self.engine._run_loop('Use record_tool then finish.', sys_print_callback=lambda _: None)
        self.assertTrue(result.ok, result)
        self.assertEqual(result.text, 'DONE')
        self.assertEqual(self.tool.calls, ['after'])
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]['reasoning'], {'effort': 'high'})
        self.assertNotIn('max_tokens', requests[0])
        self.assertNotIn('reasoning_effort', requests[0])
        self.assertTrue(all(not tool['strict'] for tool in requests[0]['tools']))

    async def test_summary_uses_responses_and_rejects_incomplete_output(self):
        create = AsyncMock(return_value=NS(status='completed', output_text='Evidence retained.', usage=NS(input_tokens=30, output_tokens=10)))
        client = NS(base_url=BASE, responses=NS(create=create))
        result = await safe_llm_call(client, 'gpt-6.1-sol', 'openai', [{'role': 'user', 'content': 'Summarize.'}], 1000)
        self.assertEqual(result, 'Evidence retained.')
        self.assertEqual(create.await_args.kwargs['max_output_tokens'], 1000)
        create.return_value.status = 'incomplete'
        with self.assertRaises(ValueError):
            await safe_llm_call(client, 'gpt-6.1-sol', 'openai', [], 1000)
