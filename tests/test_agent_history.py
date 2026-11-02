"""Read saved native/CLI dialogue without running models or rebasing a writer."""
import base64
import io
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
import uuid
from unittest.mock import patch

from PIL import Image
from xuloom.core.storage import atomic_json, history_root, artifact_directory
from xuloom.features.agent_history import read, image


class AgentHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='xuloom-history-reader-')
        self.cwd = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'XULOOM_DATA_DIR': str(self.cwd / 'data')})
        self.env.start()
        self.parent, self.agent = 'parent-owned', 'child-owned'
        self.root = history_root(self.cwd)
        self.parent_path = self.root / 'sessions' / (self.parent + '.json')
        self.child_path = self.root / 'sessions' / 'subagents' / (self.agent + '.json')
        self.child = {'session_id': self.parent, 'agent_id': self.agent, 'messages': [],
            'metadata': {'delegation': {'owner_session_id': self.parent}}, 'revision': 7}
        atomic_json(self.parent_path, {'messages': [], 'metadata': {'subagents': [{'agent_id': self.agent, 'status': 'completed'}]}})
        self.save()

    def tearDown(self):
        self.env.stop()
        from tests.owned_cleanup import remove_fixture
        remove_fixture(self.cwd, 'xuloom-history-reader-')
        self.temp.cleanup()

    def save(self):
        atomic_json(self.child_path, self.child)

    def load(self, **values):
        return read(self.cwd, self.parent, self.agent, **values)

    def journal(self, backend, rows):
        sid = str(uuid.uuid4())
        home = artifact_directory(self.cwd) / self.parent / 'external' / sid
        folder = home / ('config/projects/owned' if backend == 'claude' else 'config/sessions/day' if backend == 'codex' else 'sessions')
        path = folder / (sid + '.jsonl')
        self.child['metadata']['external_agent'] = {'backend': backend, 'session_id': sid, 'phase': 'idle',
            'session_file': str(path), 'codex_thread_id': sid, 'dsh_session_id': 'session-' + sid}
        header = {'type': 'session', 'version': 3 if backend == 'pi' else 4, 'id': sid if backend == 'pi' else 'session-' + sid}
        if backend == 'codex': header = {'type': 'session_meta', 'payload': {'id': sid}}
        if backend == 'claude': header = {'type': 'atis-latch', 'sessionId': sid}
        path.parent.mkdir(parents=True)
        path.write_text('\n'.join(json.dumps(r) for r in [header, *rows]) + '\n', encoding='utf8')
        self.save()
        return path

    def test_native_dialogue_tools_thinking_and_unknown_content(self):
        self.child['messages'] = [
            {'role': 'system', 'content': 'PRIVATE_CONFIGURATION'},
            {'role': 'user', 'content': 'Task'},
            {'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': 'Public reasoning'},
                {'type': 'tool_use', 'id': 'one', 'name': 'read', 'input': {'file': 'a.txt'}}]},
            {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'one', 'is_error': True, 'content': 'Denied'}]},
            {'role': 'assistant', 'content': [{'type': 'text', 'text': 'Answer'}, {'type': 'unknown', 'secret': 'DO_NOT_PROJECT'}]}]
        self.save()
        result = self.load()
        self.assertEqual([r['kind'] for r in result['entries']], ['text', 'thinking', 'tool_call', 'tool_result', 'text', 'notice'])
        self.assertTrue(result['entries'][3]['error'])
        self.assertNotIn('PRIVATE_CONFIGURATION', json.dumps(result))
        self.assertNotIn('DO_NOT_PROJECT', json.dumps(result))

    def test_large_single_message_pages_without_truncation_or_duplicates(self):
        text = '一段完整内容\n' * 40000
        self.child['messages'] = [{'role': 'assistant', 'content': text}]; self.save()
        cursor, output, pages = None, '', 0
        while True:
            page = self.load(cursor=cursor, limit=3); pages += 1
            output += ''.join(r['text'] for r in page['entries'])
            self.assertLessEqual(len(page['entries']), 3)
            cursor = page['nextCursor']
            if not cursor: break
        self.assertGreater(pages, 5); self.assertEqual(output, text)

    def test_cursor_is_bound_to_content_and_parent_not_a_path(self):
        self.child['messages'] = [{'role': 'user', 'content': 'a'}, {'role': 'assistant', 'content': 'b'}]; self.save()
        cursor = self.load(limit=1)['nextCursor']
        self.child['messages'].append({'role': 'assistant', 'content': 'new'}); self.save()
        with self.assertRaisesRegex(ValueError, 'changed'): self.load(cursor=cursor)
        for bad in ('../../secret', base64.urlsafe_b64encode(b'{"index":true}').decode(), 'x' * 513):
            with self.assertRaises(ValueError): self.load(cursor=bad)

    def test_registered_parent_and_child_ownership_are_required(self):
        with self.assertRaises(ValueError): read(self.cwd, self.parent, 'other')
        for field in ('agent_id', 'session_id'):
            original = self.child[field]; self.child[field] = 'different'; self.save()
            with self.assertRaises(ValueError): self.load()
            self.child[field] = original
        self.child['metadata']['delegation']['owner_session_id'] = 'different'; self.save()
        with self.assertRaises(ValueError): self.load()
        for value in ('../parent', '', True):
            with self.assertRaises(ValueError): read(self.cwd, value)

    def test_no_write_migration_or_session_manager_on_read(self):
        from xuloom.core.session import SessionManager
        self.child['messages'] = [{'role': 'assistant', 'content': 'Saved'}]; self.save()
        before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob('*') if p.is_file()}
        with patch.object(SessionManager, 'load_subagent_transcript', side_effect=AssertionError('Reader rebased writer')):
            self.load()
        after = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_vendor_journals_preserve_committed_text_and_tool_order(self):
        variants = {
            'claude': [{'type': 'user', 'message': {'role': 'user', 'content': 'Task'}},
                {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'name': 'read', 'id': 'call', 'input': {}}]}},
                {'type': 'user', 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call', 'content': 'Evidence'}]}},
                {'type': 'assistant', 'message': {'role': 'assistant', 'content': 'Answer'}}],
            'pi': [{'type': 'message', 'message': {'role': 'user', 'content': 'Task'}},
                {'type': 'message', 'message': {'role': 'assistant', 'content': [{'type': 'toolCall', 'name': 'read', 'id': 'call', 'arguments': {}}]}},
                {'type': 'message', 'message': {'role': 'toolResult', 'toolCallId': 'call', 'content': 'Evidence'}},
                {'type': 'message', 'message': {'role': 'assistant', 'content': 'Answer'}}],
            'dsh': [{'type': 'user/message', 'data': {'role': 'user', 'content': 'Task'}},
                {'type': 'assistant/message', 'data': {'message': {'role': 'assistant', 'content': [{'type': 'tool-call', 'name': 'read', 'id': 'call', 'arguments': '{}'}]}}},
                {'type': 'tool/call', 'data': {'name': 'read', 'callId': 'call'}},
                {'type': 'tool/result', 'data': {'message': {'role': 'tool', 'toolCallId': 'call', 'content': 'Evidence'}}},
                {'type': 'assistant/message', 'data': {'message': {'role': 'assistant', 'content': 'Answer'}}}],
            'codex': [{'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': 'Task'}},
                {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'read', 'call_id': 'call', 'arguments': '{}'}},
                {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'call', 'output': 'Evidence'}},
                {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'content': 'Answer'}}],
        }
        for backend, rows in variants.items():
            with self.subTest(backend=backend):
                self.journal(backend, rows)
                result = self.load()
                self.assertEqual([r['kind'] for r in result['entries']], ['text', 'tool_call', 'tool_result', 'text'])
                self.assertEqual(result['entries'][-1]['text'], 'Answer')

    def test_unrelated_external_path_and_changed_header_are_refused(self):
        path = self.journal('pi', [])
        self.child['metadata']['external_agent']['session_file'] = str(self.parent_path); self.save()
        with self.assertRaises(ValueError): self.load()
        self.child['metadata']['external_agent']['session_file'] = str(path); self.save()
        path.write_text('{"type":"session","version":3,"id":"foreign"}\n', encoding='utf8')
        with self.assertRaisesRegex(ValueError, 'header'): self.load()

    def test_live_partial_record_is_visible_but_idle_corruption_is_not_hidden(self):
        path = self.journal('pi', [{'type': 'message', 'message': {'role': 'assistant', 'content': 'Saved'}}])
        with path.open('ab') as stream: stream.write(b'{"type":')
        with self.assertRaisesRegex(ValueError, 'incomplete'): self.load()
        self.child['metadata']['external_agent']['phase'] = 'running'; self.save()
        page = self.load()
        self.assertTrue(page['partial']); self.assertTrue(page['live'])
        self.assertEqual(page['entries'][0]['text'], 'Saved')

    def test_images_are_lazy_owned_and_do_not_download_addresses(self):
        output = io.BytesIO(); Image.new('RGB', (3, 2), 'blue').save(output, format='PNG')
        encoded = base64.b64encode(output.getvalue()).decode()
        self.child['messages'] = [{'role': 'user', 'content': [{'type': 'image', 'source': {'type': 'base64', 'data': encoded}}]}]; self.save()
        page = self.load(); self.assertNotIn(encoded, json.dumps(page))
        value = image(self.cwd, self.parent, self.agent, page['entries'][0]['imageIndex'], page['version'])
        self.assertEqual((value['width'], value['height']), (3, 2))
        with self.assertRaises(ValueError): image(self.cwd, self.parent, self.agent, 0, 'stale')
        self.child['messages'][0]['content'] = [{'type': 'image_url', 'image_url': {'url': 'https://private.invalid/a.png'}}]; self.save()
        with self.assertRaisesRegex(ValueError, 'downloaded'):
            image(self.cwd, self.parent, self.agent, 0, self.load()['version'])

    def test_invalid_limits_and_queued_workers(self):
        for value in (True, 0, 101, '2'):
            with self.assertRaises(ValueError): self.load(limit=value)
        self.child_path.unlink()
        atomic_json(self.parent_path, {'messages': [], 'metadata': {'subagents': [{'agent_id': self.agent, 'status': 'queued'}]}})
        self.assertEqual(self.load()['entries'], [])

    def test_read_does_not_mask_an_optimistic_write_conflict(self):
        from xuloom.core.session import SessionManager
        with SessionManager(session_dir=self.root / 'sessions', cwd=self.cwd) as manager:
            original = manager.load_subagent_transcript(self.agent)
            versions = dict(manager._versions)
            self.child['revision'] = 8
            self.child['messages'] = [{'role': 'assistant', 'content': 'newer writer'}]; self.save()
            self.assertEqual(self.load()['entries'][0]['text'], 'newer writer')
            self.assertEqual(manager._versions, versions)
            self.assertFalse(manager.save_subagent_transcript(self.agent, original))
            self.assertEqual(json.loads(self.child_path.read_bytes())['revision'], 8)

    def test_formal_cli_reads_saved_child_with_pure_json_and_no_execution(self):
        import xuloom
        self.child['messages'] = [{'role': 'assistant', 'content': 'CLI_完整记录'}]; self.save()
        # Use this exact tested package also when the interpreter's installed copy
        # has not yet been replaced by the staged resource update.
        entry = "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('xuloom',run_name='__main__')"
        result = subprocess.run([sys.executable, '-I', '-c', entry, str(Path(xuloom.__file__).resolve().parent.parent),
            'transcript', '--cwd', str(self.cwd),
            '--session', self.parent, '--agent', self.agent, '--json'], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode('utf8', errors='replace'))
        rows = [json.loads(line) for line in result.stdout.decode('utf8').splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['result']['transcript']['entries'][0]['text'], 'CLI_完整记录')

    def test_shell_history_arguments_share_the_read_only_endpoint(self):
        from xuloom.features.agent_history import command_params
        self.assertEqual(command_params(''), {'agentId': None, 'cursor': None})
        self.assertEqual(command_params('- token'), {'agentId': None, 'cursor': 'token'})
        self.assertEqual(command_params('child token'), {'agentId': 'child', 'cursor': 'token'})
        self.assertEqual(command_params('ChildID AbC+/='), {'agentId': 'ChildID', 'cursor': 'AbC+/='})
        with self.assertRaises(ValueError): command_params('../elsewhere')
        with self.assertRaises(ValueError): command_params('one two three')

    def test_corrupt_metadata_and_messages_are_not_treated_as_empty_history(self):
        for value in (None, [], 'broken'):
            self.child['metadata'] = value; self.save()
            with self.assertRaises(ValueError): self.load()
        self.child['metadata'] = {'delegation': {'owner_session_id': self.parent}}
        self.child['messages'] = None; self.save()
        with self.assertRaises(ValueError): self.load()

    def test_oversized_saved_json_is_refused_before_decoding(self):
        from xuloom.features.agent_history import MAX_HISTORY
        with self.child_path.open('wb') as stream: stream.truncate(MAX_HISTORY + 1)
        with self.assertRaisesRegex(ValueError, '256 MiB'): self.load()


if __name__ == '__main__': unittest.main()
