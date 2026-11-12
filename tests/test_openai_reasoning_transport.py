"""Strict thinking/tool contracts over the actual OpenAI SDK; no remote requests."""
import copy
import importlib
import json
import unittest
from pathlib import Path
from tests import test_tool_recovery as recovery
from xuloom.desktop.turns import conversation_history


class OpenAIReasoningTransportTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = recovery.RecoveryTests.asyncSetUp
    asyncTearDown = recovery.RecoveryTests.asyncTearDown

    def strict_service(self, completions):
        client = self.engine.client._client
        module = next(c.__module__.split('.')[0] for c in type(client).__mro__
                      if c.__module__.split('.')[0] in {'httpx', 'httpx2'})
        wire = importlib.import_module(module)
        self.requests = []

        def handler(request):
            body = json.loads(request.content); self.requests.append(body)
            if body.get('tool_choice') not in (None, 'auto', 'none'):
                return wire.Response(400, json={'error': {'message': 'Thinking mode does not support this tool_choice', 'type': 'invalid_request_error'}})
            for message in body['messages']:
                if message['role'] == 'assistant' and 'reasoning_content' not in message:
                    return wire.Response(400, json={'error': {'message': 'The reasoning_content in the thinking mode must be passed back to the API.', 'type': 'invalid_request_error'}})
            item = completions[len(self.requests) - 1]
            if isinstance(item, tuple):
                name, values = item
                delta = {'tool_calls': [{'index': 0, 'id': 'strict-' + str(len(self.requests)), 'type': 'function',
                    'function': {'name': name, 'arguments': json.dumps(values)}}]}
                finish = 'tool_calls'
            else:
                delta, finish = {'content': item}, 'stop'
            chunks = [{'choices': [{'index': 0, 'delta': {'reasoning_content': 'PRIVATE_TRANSPORT_REASONING'}, 'finish_reason': None}]},
                      {'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 5}}]
            return wire.Response(200, headers={'content-type': 'text/event-stream'},
                                 text=''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks) + 'data: [DONE]\n\n')
        client._transport = wire.MockTransport(handler)

    async def run_task(self, text='Owned task'):
        async def event(value): self.events.append(value)
        return await self.engine._run_loop(text, sys_print_callback=lambda _: None, event_callback=event)

    async def test_plain_greeting_in_ultra_needs_no_assessment_tool_or_repair(self):
        self.engine.set_collaboration_mode('ultra')
        self.strict_service(['Hello', 'Hello', 'Hello'])
        result = await self.run_task('你好')
        self.assertTrue(result.ok, result.text)
        self.assertEqual(result.text, 'Hello')
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn('ultra_answer', {t['function']['name'] for t in self.requests[0]['tools']})
        self.assertFalse(any(e['type'] in {'tool_protocol_error', 'tool_start'} for e in self.events))
        self.assertTrue(all(e.get('channel') != 'commentary' for e in self.events if e['type'] == 'text_delta'))

    async def test_ultra_thinking_uses_supported_auto_and_keeps_assessment_guard(self):
        self.engine.set_collaboration_mode('ultra')
        self.engine.reasoning_profile = {'adapter': 'deepseek', 'thinking': 'enabled', 'efforts': [], 'canDisable': True}
        self.strict_service(['SIMPLE', 'SECOND'])
        result = await self.run_task('Hello')
        self.assertTrue(result.ok, result.text)
        self.assertEqual(result.text, 'SIMPLE')
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.requests[0]['tool_choice'], 'auto')
        self.assertEqual(self.requests[0]['thinking'], {'type': 'enabled'})
        self.assertEqual({x['function']['name'] for x in self.requests[0]['tools']}, {'ultra_prepare'})
        result = await self.run_task('Hello again')
        self.assertTrue(result.ok, result.text)
        self.assertEqual(result.text, 'SECOND')

    async def test_reasoning_roundtrips_tools_and_plain_replies_after_json_reload(self):
        self.engine.set_collaboration_mode('auto')
        self.strict_service([('record_tool', {'value': 'ONCE'}), 'DONE', 'FOLLOWUP'])
        result = await self.run_task()
        self.assertTrue(result.ok, result.text)
        self.assertEqual(self.tool.calls, ['ONCE'])
        saved = json.loads(Path(self.engine.session_manager.last_save_path).read_text(encoding='utf-8'))
        self.engine.messages = saved['messages']; self.engine._sync_message_context()
        self.engine.tool_context['request_model'] = 'previous-fallback'
        result = await self.run_task('Continue discussing the saved result')
        self.assertTrue(result.ok, result.text)
        self.assertEqual(result.text, 'FOLLOWUP')
        self.assertEqual(self.tool.calls, ['ONCE'])
        for body in self.requests[1:]:
            previous = [m for m in body['messages'] if m['role'] == 'assistant']
            self.assertTrue(previous)
            self.assertTrue(all(m['reasoning_content'] == 'PRIVATE_TRANSPORT_REASONING' for m in previous))
        self.assertNotIn('PRIVATE_TRANSPORT_REASONING', json.dumps(self.events))
        self.assertNotIn('PRIVATE_TRANSPORT_REASONING', json.dumps(conversation_history(self.engine.messages)))
        messages = copy.deepcopy(self.engine.messages)
        self.engine.tool_context.pop('request_model', None)
        for attribute, value in [('api_base_url', 'https://different.invalid/v1'), ('_api_key', 'other-key'), ('primary_model', 'other-model')]:
            original = getattr(self.engine, attribute)
            setattr(self.engine, attribute, value)
            self.assertTrue(all('reasoning_content' not in m for m in self.engine._clean_roles_for_openai_api(messages, '')))
            setattr(self.engine, attribute, original)
        from types import SimpleNamespace
        from xuloom.utils.vcr import VCRReplayResponse
        from xuloom.testing.models import response_from_record
        state = next(m['_openai_reasoning'] for m in messages if '_openai_reasoning' in m)
        payload = self.engine._serialize_response_for_vcr(SimpleNamespace(content=[], stop_reason='end_turn', usage=None, _openai_reasoning=state))
        self.assertEqual(VCRReplayResponse(payload)._openai_reasoning, state)
        self.assertEqual(response_from_record(payload)._openai_reasoning, state)
