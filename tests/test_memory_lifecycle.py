"""Owned memory jobs, durable acknowledgement and conversation snapshots."""
import asyncio
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from xuloom.core.engine import QueryEngine
from xuloom.memory.memory import MemoryCompactor
from xuloom.memory.session_memory import SessionMemoryConfig


class MemoryLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(); self.root = Path(self.directory.name)
        config = self.root / 'config.json'; config.write_text('{"model_connections": []}')
        self.patches = ExitStack()
        self.patches.enter_context(patch.dict(os.environ, {
            'HOME': str(self.root/'home'), 'USERPROFILE': str(self.root/'home'),
            'XULOOM_DATA_DIR': str(self.root/'data'), 'XULOOM_SKILLS_PATHS': '',
            'OPENAI_API_KEY': 'fixture', 'OPENAI_BASE_URL': 'http://127.0.0.1:9/v1',
            'XULOOM_MODEL_PROVIDER': 'openai',
        }))
        self.patches.enter_context(patch('xuloom.core.config.CONFIG_FILE', config))
        self.patches.enter_context(patch.object(MemoryCompactor, '_resolve_token_encoder', return_value=None))
        self.engine = QueryEngine(model='fixture', model_provider='openai', api_key='fixture',
            api_base_url='http://127.0.0.1:9/v1', cwd=str(self.root))
        self.extractor = self.engine.memory_extractor
        self.summary = self.engine.session_memory_manager
        self.summary.config = SessionMemoryConfig(0, 0, 0)
        self.engine.require_writer()
        self.messages = [{'role': 'user', 'content': 'Save this verified preference.', '_uuid': 'first'}]

    async def asyncTearDown(self):
        await self.engine.aclose()
        self.patches.close(); self.directory.cleanup()

    async def job(self, manager):
        before = asyncio.all_tasks()
        if manager is self.extractor:
            manager.record_turn(); await manager.maybe_extract(self.messages)
        else:
            manager.record_tool_call(); await manager.maybe_extract(self.messages, 1000)
        jobs = asyncio.all_tasks() - before
        self.assertEqual(len(jobs), 1)
        return jobs.pop()

    async def test_engine_close_finishes_owned_memory_before_closing_clients_or_releasing_writer(self):
        entered, release, exited = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def blocked(*args):
            entered.set()
            try: await release.wait()
            finally: exited.set()
            return [{'title': 'Verified preference', 'content': 'Respond concisely'}]
        with patch.object(self.extractor, '_call_llm_for_extraction', side_effect=blocked):
            job = await self.job(self.extractor)
            try:
                await asyncio.wait_for(entered.wait(), 2)
                await self.engine.aclose()
                self.assertTrue(exited.is_set(), 'Memory request outlived engine cleanup')
                self.assertTrue(job.done())
                self.assertFalse((Path(self.extractor.memory_dir)/'verified_preference.md').exists())
            finally:
                release.set(); await asyncio.gather(job, return_exceptions=True)

    async def test_failed_session_summary_retains_notes_and_unacknowledged_watermark(self):
        Path(self.summary.memory_path).write_text('ORIGINAL NOTES', encoding='utf-8')
        with patch.object(self.summary, '_call_llm_for_update', AsyncMock(return_value=None)):
            await self.job(self.summary)
            await asyncio.sleep(0)
        self.assertEqual(Path(self.summary.memory_path).read_text(encoding='utf-8'), 'ORIGINAL NOTES')
        self.assertEqual(self.summary._tokens_at_last_extraction, 0)
        self.assertIsNone(self.summary._last_summarised_msg_uuid)
        self.assertEqual(self.summary._tool_calls_since_update, 1)

    async def test_failed_durable_extraction_does_not_acknowledge_the_turn(self):
        with patch.object(self.extractor, '_call_llm_for_extraction', AsyncMock(return_value=None)):
            job = await self.job(self.extractor); await job
        self.assertIsNone(self.extractor._last_extracted_msg_uuid)
        self.assertEqual(self.extractor._turns_since_last_extraction, 1)

    async def test_snapshots_and_inflight_counters_do_not_acknowledge_later_messages(self):
        for manager in (self.extractor, self.summary):
            self.messages = [{'role':'user', 'content':[{'type':'text','text':'ORIGINAL SNAPSHOT'}], '_uuid':'first'}]
            entered, release = asyncio.Event(), asyncio.Event()
            calls = []
            async def blocked(context, *_):
                calls.append(context); entered.set(); await release.wait()
                return [] if manager is self.extractor else 'SAVED NOTES'
            method = '_call_llm_for_extraction' if manager is self.extractor else '_call_llm_for_update'
            with patch.object(manager, method, side_effect=blocked):
                job = await self.job(manager)
                self.messages[0]['content'][0]['text'] = 'MUTATED LATER'
                self.messages.append({'role':'user', 'content':'LATER MESSAGE', '_uuid':'second'})
                manager.record_turn() if manager is self.extractor else manager.record_tool_call()
                await asyncio.wait_for(entered.wait(), 2)
                if manager is self.extractor:
                    await manager.maybe_extract(self.messages)
                else:
                    await manager.maybe_extract(self.messages, 2000)
                release.set(); await job
            self.assertIn('ORIGINAL SNAPSHOT', calls[0]); self.assertNotIn('MUTATED LATER', calls[0])
            self.assertNotIn('LATER MESSAGE', calls[0]); self.assertEqual(len(calls), 1)
            cursor = manager._last_extracted_msg_uuid if manager is self.extractor else manager._last_summarised_msg_uuid
            count = manager._turns_since_last_extraction if manager is self.extractor else manager._tool_calls_since_update
            self.assertEqual(cursor, 'first'); self.assertEqual(count, 1)

    async def test_unchanged_title_updates_in_place_and_save_failure_keeps_cursor(self):
        path = Path(self.extractor.memory_dir)/'verified_preference.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('---\ntitle: Verified preference\n---\nOLD MEMORY', encoding='utf-8')
        content = [{'title':'Verified preference', 'content':'NEW MEMORY'}]
        with patch.object(self.extractor, '_call_llm_for_extraction', AsyncMock(return_value=content)):
            with patch('xuloom.core.storage.os.replace', side_effect=OSError('controlled disk failure')):
                job = await self.job(self.extractor); await job
            self.assertIn('OLD MEMORY', path.read_text(encoding='utf-8'))
            self.assertIsNone(self.extractor._last_extracted_msg_uuid)
            self.assertEqual(self.extractor.last_error, 'extraction_failed')
            job = await self.job(self.extractor); await job
        self.assertIn('NEW MEMORY', path.read_text(encoding='utf-8'))
        self.assertEqual(sorted(p.name for p in path.parent.glob('verified_preference*.md')), [path.name])
        self.assertEqual(self.extractor._last_extracted_msg_uuid, 'first')

    async def test_checkpoint_failure_after_file_write_is_not_acknowledged_and_can_retry(self):
        for manager in (self.extractor, self.summary):
            result = [{'title':'Stable memory', 'content':'VERIFIED'}] if manager is self.extractor else 'VERIFIED NOTES'
            method = '_call_llm_for_extraction' if manager is self.extractor else '_call_llm_for_update'
            with patch.object(manager, method, AsyncMock(return_value=result)):
                with patch.object(self.engine, 'persist_session_state', return_value=False):
                    job = await self.job(manager); await job
                self.assertEqual(manager.last_error, 'checkpoint_failed')
                cursor = manager._last_extracted_msg_uuid if manager is self.extractor else manager._last_summarised_msg_uuid
                self.assertIsNone(cursor)
                job = await self.job(manager); await job
            self.assertIsNone(manager.last_error)
            state = self.engine.session_manager.load_session_state(self.engine.session_id)['metadata']
            key, cursor = ('memory_extractor','last_extracted_msg_uuid') if manager is self.extractor else ('session_memory','last_summarised_msg_uuid')
            self.assertEqual(state[key][cursor], 'first')

    async def test_model_switch_revokes_even_a_request_that_suppresses_cancellation(self):
        entered = asyncio.Event()
        async def suppress(*_):
            entered.set()
            try: await asyncio.Event().wait()
            except asyncio.CancelledError: return [{'title':'STALE', 'content':'Do not write'}]
        with patch.object(self.extractor, '_call_llm_for_extraction', side_effect=suppress):
            job = await self.job(self.extractor); await asyncio.wait_for(entered.wait(), 2)
            self.engine.switch_model_runtime(model='new-fixture', persist=False)
            await job
        self.assertFalse((Path(self.extractor.memory_dir)/'stale.md').exists())
        self.assertIsNone(self.extractor._last_extracted_msg_uuid)

    async def test_session_rebinding_retains_ownership_of_cancelled_summary_until_close(self):
        entered = asyncio.Event()
        async def blocked(*_):
            entered.set(); await asyncio.Event().wait()
        with patch.object(self.summary, '_call_llm_for_update', side_effect=blocked):
            job = await self.job(self.summary); await asyncio.wait_for(entered.wait(), 2)
            self.summary.bind_session('other-session', {})
            self.assertEqual(self.summary._tokens_at_last_extraction, 0)
            await self.engine.aclose()
            self.assertTrue(job.done())
        with patch.object(self.summary, '_call_llm_for_update', AsyncMock()) as call:
            await self.summary.maybe_extract(self.messages, 1000)
            await asyncio.sleep(0)
            call.assert_not_awaited()

    async def test_unreadable_existing_summary_never_calls_model_or_overwrites_old_notes(self):
        Path(self.summary.memory_path).write_text('ORIGINAL EVIDENCE', encoding='utf-8')
        with patch('xuloom.memory.session_memory.IOPath.open', side_effect=PermissionError('controlled read failure')):
            with patch.object(self.summary, '_call_llm_for_update', AsyncMock(return_value='UNWANTED REPLACEMENT')) as call:
                job = await self.job(self.summary); await job
                call.assert_not_awaited()
        self.assertEqual(Path(self.summary.memory_path).read_text(encoding='utf-8'), 'ORIGINAL EVIDENCE')
        self.assertIsNone(self.summary._last_summarised_msg_uuid)

    async def test_sdk_request_can_be_stopped_then_retried_without_replaying_or_leaving_a_job(self):
        started, release = threading.Event(), threading.Event()
        records = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                records.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                started.set(); release.wait(8)
                body = json.dumps({'id':'memory-fixture','object':'chat.completion','model':'fixture',
                    'choices':[{'index':0,'message':{'role':'assistant','content':'[]'},'finish_reason':'stop'}],
                    'usage':{'prompt_tokens':10,'completion_tokens':2,'total_tokens':12}}).encode()
                try:
                    self.send_response(200); self.send_header('Content-Type','application/json')
                    self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
                except OSError: pass
        server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
        thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            self.engine.switch_model_runtime(api_base_url=f'http://127.0.0.1:{server.server_port}/v1', persist=False)
            job = await self.job(self.extractor)
            self.assertTrue(await asyncio.to_thread(started.wait, 5))
            await self.engine.request_abort()
            self.assertTrue(job.done()); self.assertTrue(job.cancelled())
            self.assertIsNone(self.extractor._last_extracted_msg_uuid)
            self.assertEqual(len(records), 1)
            release.set()
            job = await self.job(self.extractor); await asyncio.wait_for(job, 5)
            self.assertEqual(len(records), 2)
            self.assertEqual(self.extractor._last_extracted_msg_uuid, 'first')
            self.assertIsNone(self.extractor.last_error)
        finally:
            release.set(); await asyncio.to_thread(server.shutdown)
            server.server_close(); thread.join(3)

    async def test_stop_during_final_hook_does_not_start_new_memory_or_report_completion(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def create(**_):
            async def stream():
                yield NS(choices=[NS(delta=NS(content='FINAL DRAFT', tool_calls=[]), finish_reason='stop')],
                    usage=NS(prompt_tokens=10, completion_tokens=5))
            return stream()
        async def final_hook(**_):
            entered.set(); await release.wait()
        with patch.object(self.engine.client.chat.completions, 'create', side_effect=create):
            with patch.object(self.engine, '_apply_post_sampling_hooks', side_effect=final_hook):
                with patch.object(self.extractor, 'maybe_extract', AsyncMock()) as durable:
                    with patch.object(self.summary, 'maybe_extract', AsyncMock()) as summary:
                        run = asyncio.create_task(self.engine.run('Answer directly.', lambda _: None))
                        try:
                            await asyncio.wait_for(entered.wait(), 3)
                            stop = asyncio.create_task(self.engine.request_abort())
                            await asyncio.sleep(0); release.set()
                            result = await asyncio.wait_for(run, 3); await stop
                            self.assertEqual(result.reason, 'cancelled', result.to_dict())
                            durable.assert_not_awaited(); summary.assert_not_awaited()
                        finally:
                            release.set(); await asyncio.gather(run, return_exceptions=True)
