"""Reading receipts must describe the current bytes and selected pages."""
import base64
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    import pymupdf
except ImportError:  # optional "pdf" extra
    pymupdf = None
from xuloom.context.file_state_cache import FileStateCache
from xuloom.tools.file_read_tool import FileReadTool


class ReadingContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.tool = FileReadTool()
        self.tool.context = {"file_state_cache": FileStateCache(), "model_provider": "openai",
                             "model_input_modalities": ["text", "image"]}

    async def test_changed_version_does_not_inherit_other_read_ranges(self):
        p = self.root / 'data.txt'; p.write_text('old first\nold second\n')
        for offset in (1, 2): await self.tool.execute(str(p), offset=offset, limit=1)
        p.write_text('new first\nnew second\n')
        await self.tool.execute(str(p), offset=1, limit=1)
        self.assertIn('new second', await self.tool.execute(str(p), offset=2, limit=1))

    async def test_force_can_restore_evidence_removed_from_model_context(self):
        p = self.root / 'evidence.txt'; p.write_text('EXACT SOURCE')
        await self.tool.execute(str(p), offset=1, limit=1)
        self.assertIn('EXACT SOURCE', await self.tool.execute(str(p), offset=1, limit=1, force=True))

    async def test_empty_file_read_authorizes_existing_write_guard(self):
        from xuloom.tools.file_write_tool import FileWriteTool
        p = self.root/'empty.txt'; p.write_text('')
        await self.tool.execute(str(p))
        writer = FileWriteTool(); writer.context = self.tool.context
        result = await writer.execute(str(p), 'now contains evidence')
        self.assertTrue(result['ok'], result)
        self.assertEqual(p.read_text(), 'now contains evidence')

    async def test_own_write_does_not_assign_old_line_ranges_to_new_version(self):
        from xuloom.tools.file_write_tool import FileWriteTool
        p = self.root/'shifted.txt'; p.write_text('first\nsecond\n')
        await self.tool.execute(str(p),offset=2,limit=1)
        writer = FileWriteTool(); writer.context = self.tool.context
        self.assertTrue((await writer.execute(str(p),'inserted\nfirst\nsecond\n'))['ok'])
        self.assertIn('2: first',await self.tool.execute(str(p),offset=2,limit=1))

    async def test_negative_offset_is_from_end_and_empty_file_is_explicit(self):
        p = self.root / 'data.txt'; p.write_text('one\ntwo\nthree\n')
        self.assertIn('3: three', await self.tool.execute(str(p), offset=-1, limit=1))
        p.write_text('')
        self.assertIn('empty', (await self.tool.execute(str(p))).lower())

    def pdf(self, image_only=False):
        p = self.root / 'paper.pdf'
        with pymupdf.open() as doc:
            for i in range(3):
                page = doc.new_page()
                if image_only: page.draw_rect(pymupdf.Rect(20, 20, 80, 80))
                else: page.insert_text((50, 50), f'PAGE_{i+1}_EVIDENCE')
            doc.save(p)
        return p

    @unittest.skipIf(pymupdf is None, "needs the pdf extra")
    async def test_pdf_route_uses_bound_context_not_process_environment(self):
        p = self.pdf()
        with patch.dict(os.environ, {'XULOOM_MODEL_PROVIDER': 'anthropic', 'ANTHROPIC_BASE_URL': ''}):
            result = await self.tool.execute(str(p), pages='2')
        self.assertIsInstance(result, str)
        self.assertIn('PAGE_2_EVIDENCE', result)
        self.assertNotIn('PAGE_3_EVIDENCE', result)

    @unittest.skipIf(pymupdf is None, "needs the pdf extra")
    async def test_native_pdf_contains_only_selected_pages(self):
        p = self.pdf(); self.tool.context.update(model_provider='anthropic', api_base_url='https://api.anthropic.com', model_input_modalities=['text','image','document'])
        result = await self.tool.execute(str(p), pages='2')
        block = next(b for b in result if b['type']=='document')
        with pymupdf.open(stream=base64.b64decode(block['source']['data']), filetype='pdf') as doc:
            self.assertEqual(len(doc), 1)
            self.assertIn('PAGE_2_EVIDENCE', doc[0].get_text())

    @unittest.skipIf(pymupdf is None, "needs the pdf extra")
    async def test_scan_warns_and_explicit_page_image_is_available(self):
        p = self.pdf(image_only=True)
        with patch.dict(os.environ, {'XULOOM_MODEL_PROVIDER':'openai'}):
            text = await self.tool.execute(str(p), pages='2', pdf_mode='text')
            self.assertIn('No extractable text', str(text))
            result = await self.tool.execute(str(p), pages='2', pdf_mode='image')
        self.assertTrue(any(b['type']=='image' for b in result))
        self.tool.context['model_input_modalities']=['text']
        self.assertIn('Error', str(await self.tool.execute(str(p), pages='2', pdf_mode='image')))

    @unittest.skipIf(pymupdf is None, "needs the pdf extra")
    async def test_invalid_pdf_ranges_are_rejected(self):
        p=self.pdf()
        for pages in ('0','3-2','1-2-3','9','-','1-9'):
            self.assertIn('Error', str(await self.tool.execute(str(p),pages=pages)),pages)

    async def test_bounded_line_read_reports_scope_and_cursor(self):
        p=self.root/'large.txt';p.write_text('line\n'*20000)
        # Assert returned scope, not full-file allocation or an invented total.
        result=await self.tool.execute(str(p),offset=1,limit=2)
        self.assertNotIn('3: line',result)
        self.assertIn('next',result.lower())

    async def test_sparse_page_handles_unicode_crlf_and_cr_across_blocks(self):
        p=self.root/'mixed.txt'
        rows=[f'行 {i}' for i in range(2500)]
        p.write_bytes(''.join(row+('\r\n' if i%2 else '\r') for i,row in enumerate(rows)).encode())
        result=await self.tool.execute(str(p),offset=2300,limit=2)
        self.assertIn('2300: 行 2299',result);self.assertNotIn('行 2301',result)
        p.write_bytes(b'x'*(1024*1024-1)+b'\r\nlast\n')
        self.assertIn('2: last',await self.tool.execute(str(p),offset=2,limit=1))

    async def test_oversized_line_requires_character_page_without_claiming_full_read(self):
        p=self.root/'long.txt';p.write_text('x'*200000,encoding='utf-8')
        result=await self.tool.execute(str(p))
        self.assertIn('char_offset',result)
        self.assertFalse(self.tool.context['file_state_cache'].has_been_read(str(p)))
        page=await self.tool.execute(str(p),char_offset=131000,char_limit=3000)
        self.assertTrue(page['ok']);self.assertTrue(page['metadata']['truncated'])

    async def test_single_integrity_scan_is_reused_for_the_read_receipt(self):
        p=self.root/'rows.txt';p.write_text('data\n'*2000,encoding='utf-8')
        cache=self.tool.context['file_state_cache']
        with patch.object(cache,'_compute_sha256',side_effect=AssertionError('second full hash scan')):
            result=await self.tool.execute(str(p),offset=1800,limit=2)
        self.assertIn('1800: data',result);self.assertTrue(cache.has_been_read(str(p)))

    def test_concurrent_mutation_rejects_page_before_recording_receipt(self):
        from types import SimpleNamespace
        p=self.root/'changing.txt';p.write_text('old\n',encoding='utf-8')
        real_fstat=os.fstat;calls=[]
        def changed(fd):
            original=real_fstat(fd);calls.append(fd)
            if len(calls)==1:return original
            return SimpleNamespace(**{key:getattr(original,key) for key in ('st_dev','st_ino','st_size','st_mtime_ns')},st_ctime_ns=original.st_ctime_ns+1)
        cache=self.tool.context['file_state_cache']
        with patch('xuloom.context.file_state_cache.os.fstat',side_effect=changed):
            with self.assertRaisesRegex(ValueError,'changed during reading'):cache.text_page(str(p),1,2)
        self.assertFalse(cache.has_been_read(str(p)))
