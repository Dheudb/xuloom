import base64
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import zipfile

from xuloom.features.document_preview import inspect
from xuloom.features.rtf_preview import extract
try:
    from preview_fixtures import create_fixtures, packaged
except ImportError:
    from tests.preview_fixtures import create_fixtures, packaged


class DocumentPreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = create_fixtures(Path(self.tmp.name) / "workspace", pdf=False)

    def read(self, name, **kwargs):
        return inspect(self.root, name, [], **kwargs)

    def test_word_paragraph_styles_tables_and_bounded_inline_image(self):
        info = self.read("report.docx", metadata=True)
        self.assertEqual(info["sections"], ["Document"])
        page = self.read("report.docx", expected_version=info["version"])
        self.assertTrue(page["blocks"][0]["heading"])
        self.assertEqual(page["blocks"][1]["spans"][0], {"text": "DOCX_BODY_PROOF", "bold": True, "italic": False})
        self.assertEqual(next(b for b in page["blocks"] if b["type"] == "table")["rows"], [["Table cell"]])
        image = next(b for b in page["blocks"] if b["type"] == "image")["source"]
        self.assertTrue(image.startswith("data:image/jpeg;base64,"))

    def test_excel_sheets_shared_strings_missing_cells_formulas_and_row_numbers(self):
        self.assertEqual(self.read("sheets.xlsx", metadata=True)["sections"], ["Measurements", "Summary"])
        page = self.read("sheets.xlsx")
        self.assertEqual(page["rows"], [["Sample", "Current"], ["XLSX_PROOF", "", "3"]])
        other = self.read("sheets.xlsx", section=1)
        self.assertEqual((other["rows"], other["rowNumbers"]), ([["SECOND_SHEET"]], ["3"]))
        with self.assertRaisesRegex(ValueError, "section"):
            self.read("sheets.xlsx", section=2)

    def test_powerpoint_uses_presentation_order_and_embedded_images(self):
        self.assertEqual(self.read("slides.pptx", metadata=True)["sections"], ["1", "2"])
        first = self.read("slides.pptx")["blocks"]
        self.assertEqual(first[0]["spans"][0]["text"], "PPTX_PROOF")
        self.assertTrue(first[1]["source"].startswith("data:image/"))
        self.assertEqual(self.read("slides.pptx", section=1)["blocks"][0]["spans"][0]["text"], "SECOND_SLIDE")

    def test_opendocument_text_table_repeats_and_presentation(self):
        for extension in ("odt", "odp"):
            self.assertEqual(self.read("open." + extension)["blocks"][0]["spans"][0]["text"], extension.upper() + "_PROOF")
        self.assertEqual(self.read("open.ods")["rows"], [["ODS_PROOF", "ODS_PROOF"], ["ODS_PROOF", "ODS_PROOF"]])

    def test_csv_quotes_utf16_tsv_and_exact_nonoverlapping_pages(self):
        first, second, last = (self.read("data.csv", offset=n) for n in (0, 50, 100))
        self.assertEqual(first["rows"][1], ["a,b", "中文"])
        self.assertEqual((first["total"], second["rows"][0], len(last["rows"])), (112, ["sample48", "48"], 12))
        self.assertEqual(self.read("data.tsv")["rows"][1], ["TSV_PROOF", "2"])

    def test_structured_text_is_formatted_without_executing_yaml_tags(self):
        self.assertIn('\n  "proof": "JSON_PROOF"', self.read("data.json")["text"])
        self.assertIn('"value": 2', self.read("data.jsonl")["text"])
        self.assertIn("YAML_PROOF", self.read("data.yaml")["text"])
        self.assertIn("XML_PROOF", self.read("data.xml")["text"])
        (self.root / "data.yaml").write_text('!!python/object/apply:os.system ["exit 1"]', encoding="utf-8")
        self.assertIn("!!python", self.read("data.yaml")["text"])

    def test_notebook_shows_markdown_source_and_text_outputs_without_html(self):
        blocks = self.read("analysis.ipynb")["blocks"]
        self.assertEqual([b["type"] for b in blocks], ["markdown", "code", "code"])
        self.assertEqual(blocks[0]["text"], "# NOTEBOOK_PROOF")
        self.assertNotIn("previewInjected", str(blocks))

    def test_rtf_unicode_and_ignored_objects(self):
        page = self.read("notes.rtf")
        self.assertIn("RTF_PROOF\n", page["text"])
        self.assertIn("中文", page["text"])
        self.assertNotIn("HIDDEN_PAYLOAD", page["text"])
        self.assertEqual(extract(br"{\rtf1\ansi\ansicpg936 \'d6\'d0\'ce\'c4}"), "中文")

    def test_archive_lists_without_extracting_parent_paths(self):
        page = self.read("files.zip")
        self.assertEqual(page["entries"][2]["name"], "../escape.txt")
        self.assertFalse((self.root.parent / "escape.txt").exists())

    def test_tiff_pages_and_ico_are_real_decodable_images(self):
        from PIL import Image
        self.assertEqual(self.read("scan.tiff", metadata=True)["pages"], 2)
        for name, section in (("scan.tiff", 0), ("scan.tiff", 1), ("icon.ico", 0)):
            result = self.read(name, section=section)
            raw = base64.b64decode(result["source"].split(",", 1)[1])
            with Image.open(io.BytesIO(raw)) as image:
                self.assertGreater(image.width, 0)

    def test_paths_versions_arguments_and_size_are_checked(self):
        outside = self.root.parent / "outside.json"
        outside.write_text("{}")
        with self.assertRaises(ValueError):
            self.read(str(outside))
        info = self.read("data.json", metadata=True)
        (self.root / "data.json").write_text('{"changed":true}')
        with self.assertRaisesRegex(ValueError, "changed"):
            self.read("data.json", expected_version=info["version"])
        for values in ({"section": -1}, {"section": True}, {"offset": -1}, {"width": 0}):
            with self.assertRaises(ValueError):
                self.read("data.json", **values)
        with patch("xuloom.features.document_preview.MAX_FILE", 1), self.assertRaisesRegex(ValueError, "64 MB"):
            self.read("data.json", metadata=True)

    def test_xml_entities_and_oversized_office_members_are_rejected(self):
        (self.root / "data.xml").write_text('<!DOCTYPE root [<!ENTITY secret SYSTEM "file:///private">]><root>&secret;</root>')
        with self.assertRaisesRegex(ValueError, "entities"):
            self.read("data.xml")
        packaged(self.root / "report.docx", {"word/document.xml": b"x" * 100})
        with patch("xuloom.features.office_preview.MAX_XML", 10), self.assertRaisesRegex(ValueError, "XML"):
            self.read("report.docx")

    def test_pdf_svg_page_sizes_and_invalid_or_protected_pdf(self):
        try:
            import pymupdf
        except ImportError:
            self.skipTest("optional PyMuPDF is unavailable")
        create_fixtures(self.root)
        self.assertEqual(self.read("paper.pdf", metadata=True)["pages"], 2)
        from PIL import Image
        for name in ("paper.pdf", "vector.svg"):
            result = self.read(name)
            with Image.open(io.BytesIO(base64.b64decode(result["source"].split(",", 1)[1]))) as image:
                self.assertLessEqual(image.width, 1200)
                self.assertLessEqual(image.height, 2200)
        with self.assertRaises(ValueError):
            self.read("paper.pdf", section=2)
        with pymupdf.open(self.root / "paper.pdf") as document:
            document.save(self.root / "protected.pdf", encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="secret")
        with self.assertRaisesRegex(ValueError, "password"):
            self.read("protected.pdf", metadata=True)

    def test_external_office_image_relationships_are_inert(self):
        target = self.root / "report.docx"
        with zipfile.ZipFile(target) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        entries["word/_rels/document.xml.rels"] = b'<Relationships><Relationship Id="image1" TargetMode="External" Target="https://invalid.example/private.png"/></Relationships>'
        packaged(target, entries)
        self.assertFalse(any(b["type"] == "image" for b in self.read("report.docx")["blocks"]))

    def test_office_macro_containers_show_only_document_content(self):
        for source, target in (("report.docx", "macro.docm"), ("sheets.xlsx", "macro.xlsm"), ("slides.pptx", "show.ppsx")):
            (self.root / target).write_bytes((self.root / source).read_bytes())
            original, alias = self.read(source), self.read(target)
            key = "rows" if original["kind"] == "table" else "blocks"
            self.assertEqual(alias[key], original[key])

    def test_table_dialects_dates_and_xml_encoding(self):
        (self.root / "semi.csv").write_text("name;value\nsample;中文", encoding="gb18030")
        self.assertEqual(self.read("semi.csv")["rows"][1], ["sample", "中文"])
        (self.root / "utf16.xml").write_text('<?xml version="1.0" encoding="utf-16"?><root>中文</root>', encoding="utf-16")
        self.assertIn("中文", self.read("utf16.xml")["text"])
        from xuloom.features.office_preview import cell_value
        self.assertEqual(cell_value("1", "0", [14], False), "1900-01-01")
        self.assertEqual(cell_value("1", "0", [14], True), "1904-01-02")
        self.assertEqual(cell_value("0.125", "0", [10], False), "12.50%")
