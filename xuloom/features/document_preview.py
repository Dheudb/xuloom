"""Read-only paged previews over the existing local/SSH file RPC."""
import base64
import csv
import io
import json
import os
import zipfile

from xuloom.features.file_changes import preview_path
from xuloom.features.media_preview import version

OFFICE = {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"}
OFFICE_ALIASES = {".docm": ".docx", ".dotx": ".docx", ".dotm": ".docx", ".xlsm": ".xlsx", ".xltx": ".xlsx", ".xltm": ".xlsx",
    ".pptm": ".pptx", ".ppsx": ".pptx", ".ppsm": ".pptx", ".potx": ".pptx", ".potm": ".pptx"}
PAGES = {".pdf", ".svg", ".xps", ".oxps", ".epub", ".fb2", ".mobi", ".cbz"}
IMAGES = {".tif", ".tiff", ".ico", ".avif"}
DATA = {".csv", ".tsv", ".json", ".jsonl", ".ndjson", ".yaml", ".yml", ".xml", ".ipynb"}
FORMATS = OFFICE | PAGES | IMAGES | DATA | set(OFFICE_ALIASES) | {".zip", ".rtf"}
MAX_FILE = 64 * 1024 * 1024
MAX_TEXT = 8 * 1024 * 1024
PAGE_ROWS = 50


def read_text(stream):
    data = stream.read(MAX_TEXT + 1)
    if len(data) > MAX_TEXT:
        raise ValueError("Structured text exceeds the 8 MB preview limit")
    if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return data.decode("utf-32")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("gb18030")


def open_pages(stream, suffix):
    try:
        import pymupdf
    except ImportError:
        raise ValueError('PDF/document rendering requires xuloom[pdf] on the execution machine') from None
    data = stream.read(MAX_FILE + 1)
    if suffix == ".svg":
        from lxml import etree
        if len(data) > MAX_TEXT:
            raise ValueError("SVG exceeds the 8 MB preview limit")
        root = etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))
        if root.getroottree().docinfo.doctype:
            raise ValueError("Document entities are not allowed in previews")
        for node in list(root.iter()):
            if not isinstance(node.tag, str):
                continue
            if etree.QName(node).localname in {"script", "foreignObject"}:
                if node.getparent() is not None:
                    node.getparent().remove(node)
                continue
            for key, value in list(node.attrib.items()):
                if key.rsplit("}", 1)[-1] == "href" and not value.startswith(("#", "data:image/png;base64,", "data:image/jpeg;base64,")):
                    del node.attrib[key]
        data = etree.tostring(root, encoding="utf-8")
    document = pymupdf.open(stream=data, filetype="xps" if suffix == ".oxps" else suffix.lstrip("."))
    if document.needs_pass:
        document.close()
        raise ValueError("This document is password protected; unlock a copy before previewing")
    return document


def sheets(text, suffix):
    from xuloom.features.office_preview import TEXT_LIMIT
    delimiter = "\t" if suffix == ".tsv" else ","
    if suffix == ".csv":
        try:
            delimiter = csv.Sniffer().sniff(text[:16000], delimiters=",;\t|").delimiter
        except csv.Error:
            pass
    # Input bytes are already bounded by MAX_TEXT; allow a long quoted field.
    csv.field_size_limit(max(csv.field_size_limit(), MAX_TEXT))
    rows = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    for index, row in enumerate(rows):
        if index >= 100000:
            raise ValueError("Table exceeds the 100000-row preview limit")
        yield [cell[:TEXT_LIMIT] for cell in row[:100]]


def notebook(text, section):
    value = json.loads(text)
    if not isinstance(value, dict) or not isinstance(value.get("cells"), list):
        raise ValueError("Invalid notebook cells")
    blocks = []
    for cell in value["cells"]:
        source = cell.get("source", "")
        source = "".join(source) if isinstance(source, list) else str(source)
        blocks.append({"type": "markdown" if cell.get("cell_type") == "markdown" else "code", "text": source[:20000]})
        for output in cell.get("outputs", [])[:10]:
            data = output.get("data", {})
            plain = output.get("text", data.get("text/plain", ""))
            if plain:
                blocks.append({"type": "code", "text": ("".join(plain) if isinstance(plain, list) else str(plain))[:12000]})
            # Notebook HTML and JavaScript outputs remain inert.
            image = data.get("image/png")
            if isinstance(image, (str, list)):
                encoded = "".join(image) if isinstance(image, list) else image
                encoded = "".join(encoded.split())
                if len(encoded) < 1_000_000:
                    try:
                        raw = base64.b64decode(encoded, validate=True)
                        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
                            from xuloom.context.tool_media import preview_image_bytes
                            blocks.append({"type": "image", "source": preview_image_bytes(raw)["source"]})
                    except (ValueError, OSError):
                        pass
        if len(blocks) > 10000:
            raise ValueError("Notebook exceeds the preview limit")
    return {"kind": "document", "blocks": blocks[section:section + PAGE_ROWS], "total": len(blocks)}


def structured(stream, suffix, offset):
    text = read_text(stream)
    if suffix in {".csv", ".tsv"}:
        rows = list(sheets(text, suffix))
        return {"kind": "table", "rows": rows[offset:offset + PAGE_ROWS], "total": len(rows)}
    if suffix == ".ipynb":
        return notebook(text, offset)
    if suffix == ".json":
        text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
    elif suffix in {".jsonl", ".ndjson"}:
        text = "\n".join(json.dumps(json.loads(line), ensure_ascii=False) for line in text.splitlines() if line.strip())
    elif suffix in {".yaml", ".yml"}:
        # Display YAML without constructing tags or expanding anchors.
        pass
    elif suffix == ".xml":
        from lxml import etree
        if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
            raise ValueError("Document entities are not allowed in previews")
        stream.seek(0)
        root = etree.fromstring(stream.read(MAX_TEXT + 1), etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))
        text = etree.tostring(root, encoding="unicode", pretty_print=True)
    lines = text.splitlines()
    return {"kind": "text", "text": "\n".join(line[:10000] for line in lines[offset:offset + 200]), "total": len(lines), "limit": 200}


def raster(stream, suffix, section, width):
    if suffix in IMAGES:
        from PIL import Image, ImageOps
        with Image.open(stream) as image:
            image.seek(section)
            if image.width * image.height > 40_000_000:
                raise ValueError("Image exceeds the 40-megapixel preview limit")
            page = ImageOps.exif_transpose(image).convert("RGB")
            page.thumbnail((width, 2200))
            output = io.BytesIO()
            page.save(output, "PNG")
            content, total = output.getvalue(), getattr(image, "n_frames", 1)
    else:
        import math
        with open_pages(stream, suffix) as document:
            total = document.page_count
            if not 0 <= section < total:
                raise ValueError("Document page is out of range")
            page = document.load_page(section)
            import pymupdf
            rect = page.rect
            if rect.width <= 0 or rect.height <= 0:
                raise ValueError("Invalid document page dimensions")
            scale = min(width / rect.width, 2200 / rect.height, math.sqrt(4_000_000 / (rect.width * rect.height)))
            content = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes("png")
    return {"kind": "page", "source": "data:image/png;base64," + base64.b64encode(content).decode("ascii"), "total": total}


def archive(stream, offset):
    with zipfile.ZipFile(stream) as package:
        entries = package.infolist()
        if len(entries) > 10000:
            raise ValueError("Archive exceeds the 10000-entry preview limit")
        return {"kind": "archive", "entries": [{"name": item.filename[:1000], "size": item.file_size, "directory": item.is_dir()} for item in entries[offset:offset + PAGE_ROWS]], "total": len(entries)}


def inspect(cwd, requested, known, *, expected_version=None, section=0, offset=0, width=1200, metadata=False):
    """Authorize on every request and reject pages from a changed file version."""
    if type(section) is not int or type(offset) is not int or section < 0 or offset < 0:
        raise ValueError("Invalid document page or row")
    if type(width) is not int or not 320 <= width <= 2000:
        raise ValueError("Invalid document preview width")
    path = preview_path(cwd, requested, known)
    original_suffix = path.suffix.lower()
    if original_suffix not in FORMATS:
        raise ValueError("This file format has no built-in document preview")
    suffix = OFFICE_ALIASES.get(original_suffix, original_suffix)
    if path.stat().st_size > MAX_FILE:
        raise ValueError("Document exceeds the 64 MB preview limit")
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        stamp = version(path, before)
        if expected_version is not None and stamp != expected_version:
            raise ValueError("File changed; refresh the preview")
        if metadata:
            result = {"kind": "structured", "sections": []}
            if suffix in OFFICE:
                from xuloom.features.office_preview import OfficeArchive
                package = OfficeArchive(stream)
                try:
                    sections = [name for name, _ in package.parts(suffix)]
                    result = {"kind": "office", "sections": sections[:1000]}
                finally:
                    package.close()
            elif suffix in PAGES:
                with open_pages(stream, suffix) as document:
                    count = document.page_count
                result = {"kind": "page", "pages": count}
            elif suffix in IMAGES:
                from PIL import Image
                with Image.open(stream) as image:
                    count = getattr(image, "n_frames", 1)
                result = {"kind": "page", "pages": count}
        elif suffix in PAGES | IMAGES:
            result = raster(stream, suffix, section, width)
        elif suffix in OFFICE:
            from xuloom.features.office_preview import OfficeArchive, content
            package = OfficeArchive(stream)
            try:
                sections = package.parts(suffix)
                if not 0 <= section < len(sections):
                    raise ValueError("Document section is out of range")
                result = content(package, suffix, sections[section][1], section, offset, PAGE_ROWS)
            finally:
                package.close()
        elif suffix == ".zip":
            result = archive(stream, offset)
        elif suffix == ".rtf":
            from xuloom.features.rtf_preview import extract
            data = stream.read(MAX_TEXT + 1)
            if len(data) > MAX_TEXT:
                raise ValueError("RTF exceeds the 8 MB preview limit")
            lines = extract(data).splitlines()
            result = {"kind": "text", "text": "\n".join(line[:10000] for line in lines[offset:offset + 200]), "total": len(lines), "limit": 200}
        else:
            result = structured(stream, suffix, offset)
        after = os.fstat(stream.fileno())
        with path.open("rb") as current:
            current_version = version(path, os.fstat(current.fileno()))
        if version(path, after) != stamp or current_version != stamp:
            raise ValueError("File changed; refresh the preview")
    return {"path": str(path), "size": after.st_size, "version": stamp, "limit": PAGE_ROWS, **result}
