"""Opt-in actual four vendor CLIs: read-only intermediate history and followup."""
import json
import base64
import os
import unittest

from tests.test_external_agents import ExternalFixture, MCP
from tests.test_codex_responses import ResponsesModel
from xuloom.features.agent_history import read, image
from xuloom.core.storage import IOPath, history_root


class InstalledHistoryTests(ExternalFixture):
    async def scenario(self, backend, images=False):
        program = os.environ.get('XULOOM_TEST_' + backend.upper() + '_PROGRAM')
        if not MCP or not program:
            self.skipTest('Set the owned installed CLI and optional MCP runtime')
        roles = self.root / '.xuloom/agents'; roles.mkdir(parents=True)
        (roles / 'external-reader.md').write_text('---\nname: external-reader\ndescription: owned history reader\nbackend: '
            + backend + '\nbackend_program: ' + program.replace('\\', '/') + '\ntools: [Read]\n---\nRead assigned evidence.\n', encoding='utf8')
        if images:
            self.mode = 'image'
            (self.root / 'evidence.png').write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j5i8AAAAASUVORK5CYII='))
        if backend == 'codex':
            self.parent_handler = ResponsesModel('namespace')
            if images: self.parent_handler.arguments = {'file_path': 'evidence.png'}
        engine = self.engine('anthropic' if backend == 'claude' else 'openai')
        result = await engine.agents('spawn', prompt='Read evidence.txt', subagent_type='external-reader')
        worker = result['data']['agents'][0]
        self.assertEqual(worker['status'], 'completed', result)
        versions = dict(engine.session_manager._versions)
        def files():
            return {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in IOPath(history_root(self.root)).rglob('*') if p.is_file()}
        before, requests = files(), len(self.records)
        cursor, entries = None, []
        while True:
            page = read(self.root, engine.session_id, worker['agent_id'], cursor=cursor, limit=2)
            entries.extend(page['entries']); cursor = page['nextCursor']
            if cursor is None: break
        self.assertEqual(files(), before)
        self.assertEqual(engine.session_manager._versions, versions)
        self.assertEqual(len(self.records), requests)
        self.assertIn('tool_call', [r['kind'] for r in entries])
        if images:
            pictures = [r for r in entries if r['kind'] == 'image']
            self.assertTrue(pictures, entries)
            value = image(self.root, engine.session_id, worker['agent_id'], pictures[0]['imageIndex'], page['version'])
            self.assertEqual((value['width'], value['height']), (1, 1))
            self.assertEqual(files(), before)
            self.assertEqual(len(self.records), requests)
        else:
            evidence = [r for r in entries if r['kind'] == 'tool_result' and 'OWNED_EVIDENCE' in r['text']]
            self.assertEqual(len(evidence), 1, entries)
        final = [r for r in entries if r['role'] == 'assistant' and r['kind'] == 'text']
        self.assertTrue(final, entries)
        self.assertIn('CONTROLLED_', final[-1]['text'])
        follow = await engine.agents('followup', agent_id=worker['agent_id'], prompt='Explain again')
        self.assertEqual(follow['data']['status'], 'completed', follow)
        after = read(self.root, engine.session_id, worker['agent_id'])
        self.assertIn('Explain again', json.dumps(after))
        self.assertGreater(len(after['entries']), len(entries))

    async def test_claude(self): await self.scenario('claude')
    async def test_pi(self): await self.scenario('pi')
    async def test_codex(self): await self.scenario('codex')
    async def test_dsh(self): await self.scenario('dsh')
    async def test_claude_images(self): await self.scenario('claude', images=True)
    async def test_pi_images(self): await self.scenario('pi', images=True)
    async def test_codex_images(self): await self.scenario('codex', images=True)
    async def test_dsh_images(self): await self.scenario('dsh', images=True)


if __name__ == '__main__': unittest.main()
