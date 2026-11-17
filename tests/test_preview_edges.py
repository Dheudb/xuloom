import tempfile
import unittest
import zipfile
from pathlib import Path

from xuloom.features.document_preview import inspect


class PreviewEdges(unittest.TestCase):
    def test_odf_tables_and_sheet_images(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ns = 'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" xmlns:xlink="http://www.w3.org/1999/xlink"'
            table = '<table:table table:name="Data"><table:table-row><table:table-cell><text:p>IMPORTANT_CELL</text:p><draw:frame><draw:image xlink:href="missing.png"/></draw:frame></table:table-cell></table:table-row></table:table>'
            for ext, body in [('odt', '<office:text>' + table + '</office:text>'), ('ods', '<office:spreadsheet>' + table + '</office:spreadsheet>')]:
                with zipfile.ZipFile(root / ('test.' + ext), 'w') as archive:
                    archive.writestr('content.xml', f'<office:document-content {ns}><office:body>{body}</office:body></office:document-content>')
                page = inspect(root, 'test.' + ext, [])
                rows = page['rows'] if ext == 'ods' else next(b['rows'] for b in page['blocks'] if b['type'] == 'table')
                self.assertEqual(rows, [['IMPORTANT_CELL']])

    def test_word_large_table_is_pageable_without_dropping_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            rows = ''.join(f'<w:tr><w:tc><w:p><w:r><w:t>ROW_{n}</w:t></w:r></w:p></w:tc></w:tr>' for n in range(1201))
            with zipfile.ZipFile(root / 'test.docx', 'w') as archive:
                archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:tbl>' + rows + '</w:tbl></w:body></w:document>')
            first = inspect(root, 'test.docx', [])
            second = inspect(root, 'test.docx', [], offset=first['limit'])
            values = [r[0] for page in (first, second) for block in page['blocks'] for r in block.get('rows', [])]
            self.assertEqual(values, [f'ROW_{n}' for n in range(1201)])

    def test_csv_long_cell_is_bounded_but_remaining_rows_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'test.csv').write_text('id,text\n1,"' + 'x' * 140000 + '"\n2,END\n', encoding='utf-8')
            page = inspect(root, 'test.csv', [])
            self.assertEqual(page['total'], 3)
            self.assertEqual(len(page['rows'][1][1]), 4000)
            self.assertEqual(page['rows'][2], ['2', 'END'])
