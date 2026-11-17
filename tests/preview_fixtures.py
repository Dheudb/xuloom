"""Small standards-based local files for preview integration tests."""
import io
import json
from pathlib import Path
import zipfile


def packaged(path, entries):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in entries.items():
            archive.writestr(name, value)


def create_fixtures(directory, *, pdf=True):
    from PIL import Image
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    image = io.BytesIO()
    Image.new("RGB", (240, 120), "#d4e7e4").save(image, "PNG")
    relns = "http://schemas.openxmlformats.org/package/2006/relationships"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    word = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    drawing = "http://schemas.openxmlformats.org/drawingml/2006/main"
    ppt = "http://schemas.openxmlformats.org/presentationml/2006/main"
    sheet = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    packaged(root / "report.docx", {
        "word/document.xml": f'<w:document xmlns:w="{word}" xmlns:r="{rel}" xmlns:a="{drawing}"><w:body>'
            '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Document preview</w:t></w:r></w:p>'
            '<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>DOCX_BODY_PROOF</w:t></w:r></w:p>'
            '<w:p><w:r><w:drawing><a:blip r:embed="image1"/></w:drawing></w:r></w:p>'
            '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Table cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
            '</w:body></w:document>',
        "word/_rels/document.xml.rels": f'<Relationships xmlns="{relns}"><Relationship Id="image1" Target="media/image.png"/></Relationships>',
        "word/media/image.png": image.getvalue(),
    })
    packaged(root / "sheets.xlsx", {
        "xl/workbook.xml": f'<workbook xmlns="{sheet}" xmlns:r="{rel}"><sheets><sheet name="Measurements" r:id="s1"/><sheet name="Summary" r:id="s2"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{relns}"><Relationship Id="s1" Target="worksheets/sheet1.xml"/><Relationship Id="s2" Target="worksheets/sheet2.xml"/></Relationships>',
        "xl/sharedStrings.xml": f'<sst xmlns="{sheet}"><si><t>Sample</t></si><si><t>XLSX_PROOF</t></si></sst>',
        "xl/worksheets/sheet1.xml": f'<worksheet xmlns="{sheet}"><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="inlineStr"><is><t>Current</t></is></c></row>'
            '<row r="2"><c r="A2" t="s"><v>1</v></c><c r="C2"><f>1+2</f><v>3</v></c></row></sheetData></worksheet>',
        "xl/worksheets/sheet2.xml": f'<worksheet xmlns="{sheet}"><sheetData><row r="3"><c r="A3" t="inlineStr"><is><t>SECOND_SHEET</t></is></c></row></sheetData></worksheet>',
    })
    packaged(root / "slides.pptx", {
        "ppt/presentation.xml": f'<p:presentation xmlns:p="{ppt}" xmlns:r="{rel}"><p:sldIdLst><p:sldId id="256" r:id="s1"/><p:sldId id="257" r:id="s2"/></p:sldIdLst></p:presentation>',
        "ppt/_rels/presentation.xml.rels": f'<Relationships xmlns="{relns}"><Relationship Id="s1" Target="slides/slide1.xml"/><Relationship Id="s2" Target="slides/slide2.xml"/></Relationships>',
        "ppt/slides/slide1.xml": f'<p:sld xmlns:p="{ppt}" xmlns:a="{drawing}" xmlns:r="{rel}"><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>PPTX_PROOF</a:t></a:r></a:p></p:txBody></p:sp><p:pic><a:blip r:embed="image1"/></p:pic></p:spTree></p:cSld></p:sld>',
        "ppt/slides/slide2.xml": f'<p:sld xmlns:p="{ppt}" xmlns:a="{drawing}"><p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>SECOND_SLIDE</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>',
        "ppt/slides/_rels/slide1.xml.rels": f'<Relationships xmlns="{relns}"><Relationship Id="image1" Target="../media/image.png"/></Relationships>',
        "ppt/media/image.png": image.getvalue(),
    })
    namespaces = 'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"'
    for extension, body in {
        "odt": '<office:text><text:h>ODT_PROOF</text:h><text:p>Open document text</text:p></office:text>',
        "ods": '<office:spreadsheet><table:table table:name="Experiment"><table:table-row table:number-rows-repeated="2"><table:table-cell table:number-columns-repeated="2"><text:p>ODS_PROOF</text:p></table:table-cell></table:table-row></table:table></office:spreadsheet>',
        "odp": '<office:presentation><draw:page draw:name="Intro"><text:p>ODP_PROOF</text:p></draw:page></office:presentation>',
    }.items():
        packaged(root / ("open." + extension), {"content.xml": f'<office:document-content {namespaces}><office:body>{body}</office:body></office:document-content>'})
    (root / "data.csv").write_text('name,value\n"a,b","中文"\n' + "".join(f"sample{i},{i}\n" for i in range(110)), encoding="utf-8-sig")
    (root / "data.tsv").write_text("name\tvalue\nTSV_PROOF\t2", encoding="utf-16")
    (root / "data.json").write_text('{"proof":"JSON_PROOF","nested":{"value":3}}', encoding="utf-8")
    (root / "data.jsonl").write_text('{"value":1}\n{"value":2}', encoding="utf-8")
    (root / "data.yaml").write_text("proof: YAML_PROOF\nvalue: true\n", encoding="utf-8")
    (root / "data.xml").write_text('<root><proof>XML_PROOF</proof></root>', encoding="utf-8")
    (root / "notes.rtf").write_bytes(br"{\rtf1\ansi RTF_PROOF\par Text \b bold\b0\par \u20013?\u25991? {\pict HIDDEN_PAYLOAD}}")
    (root / "analysis.ipynb").write_text(json.dumps({"cells": [
        {"cell_type": "markdown", "source": ["# NOTEBOOK_PROOF"]},
        {"cell_type": "code", "source": ["print(3)"], "outputs": [{"output_type": "display_data", "data": {"text/plain": ["3"], "text/html": ["<script>window.previewInjected = true</script>"]}}]},
    ]}), encoding="utf-8")
    (root / "vector.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100"><rect width="200" height="100" fill="#d4e7e4"/><text x="10" y="50">SVG_PROOF</text></svg>', encoding="utf-8")
    with Image.new("RGB", (100, 80), "green") as first, Image.new("RGB", (100, 80), "blue") as second:
        first.save(root / "scan.tiff", save_all=True, append_images=[second])
        first.save(root / "icon.ico")
    packaged(root / "files.zip", {"folder/": b"", "folder/note.txt": "ARCHIVE_PROOF", "../escape.txt": "No extraction"})
    if pdf:
        import pymupdf
        with pymupdf.open() as doc:
            for number in range(2):
                page = doc.new_page(width=420, height=600)
                page.insert_text((40, 70), f"PDF preview page {number + 1}", fontsize=22)
                page.draw_rect((40, 100, 380, 220), fill=(.83, .91, .89))
            doc.save(root / "paper.pdf")
    return root
