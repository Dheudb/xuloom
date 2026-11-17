"""Bounded, read-only content previews of ZIP-based office documents."""
import base64
import io
import posixpath
import zipfile

from lxml import etree

MAX_XML = 8 * 1024 * 1024
MAX_ITEMS = 5000
MAX_BLOCKS = 10000
TEXT_LIMIT = 4000


def local(node):
    return etree.QName(node).localname if isinstance(node.tag, str) else ""


def attr(node, key, default=""):
    return next((value for name, value in node.attrib.items() if name.endswith("}" + key)), node.get(key, default))


def descendants(node, name):
    return (child for child in node.iter() if local(child) == name)


def text(node):
    return "".join(node.itertext())[:TEXT_LIMIT]


def cell_value(value, style, styles, date_1904):
    """Recognize saved Excel dates and percentages without evaluating formulas."""
    if not value or not style.isdigit() or int(style) >= len(styles):
        return value
    number_format = styles[int(style)]
    try:
        number = float(value)
    except ValueError:
        return value
    if number_format in {9, 10}:
        return format(number * 100, ".0f" if number_format == 9 else ".2f") + "%"
    if number_format in {14, 15, 16, 17, 18, 19, 20, 21, 22, 45, 46, 47}:
        from datetime import datetime, timedelta
        try:
            date = datetime(1904, 1, 1) if date_1904 else datetime(1899, 12, 30)
            if not date_1904 and 0 < number < 60:
                number += 1
            date += timedelta(days=number)
            return date.strftime("%H:%M:%S" if number_format in {18, 19, 20, 21, 45, 46, 47} else "%Y-%m-%d %H:%M:%S" if number_format == 22 else "%Y-%m-%d")
        except (OverflowError, ValueError):
            return value
    return value


class OfficeArchive:
    def __init__(self, stream):
        self.archive = zipfile.ZipFile(stream)
        entries = self.archive.infolist()
        if len(entries) > MAX_ITEMS or sum(item.file_size for item in entries) > 128 * 1024 * 1024:
            self.archive.close()
            raise ValueError("Document package exceeds the preview limit")

    def close(self):
        self.archive.close()

    def xml(self, name):
        entry = self.archive.getinfo(name)
        if entry.file_size > MAX_XML:
            raise ValueError("Document XML exceeds the preview limit")
        data = self.archive.read(entry)
        if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
            raise ValueError("Document entities are not allowed in previews")
        return etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False))

    def relationships(self, part):
        parent, name = posixpath.split(part)
        rel = posixpath.join(parent, "_rels", name + ".rels")
        if rel not in self.archive.namelist():
            return {}
        result = {}
        for node in self.xml(rel):
            target = node.get("Target", "")
            if node.get("TargetMode") == "External" or ":" in target or "\\" in target:
                continue
            resolved = posixpath.normpath(posixpath.join(parent, target)) if not target.startswith("/") else target.lstrip("/")
            if not resolved.startswith("../") and resolved in self.archive.namelist():
                result[node.get("Id")] = resolved
        return result

    def image(self, name):
        if not name or name not in self.archive.namelist():
            return None
        entry = self.archive.getinfo(name)
        if entry.file_size > 8 * 1024 * 1024:
            return None
        from PIL import Image
        try:
            with Image.open(io.BytesIO(self.archive.read(entry))) as image:
                if image.width * image.height > 20_000_000:
                    return None
                image.thumbnail((1200, 1200))
                output = io.BytesIO()
                image.convert("RGB").save(output, format="JPEG", quality=80)
                return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")
        except (OSError, ValueError, Image.DecompressionBombError):
            return None

    def parts(self, suffix):
        if suffix == ".docx":
            return [("Document", "word/document.xml")]
        if suffix == ".pptx":
            rels = self.relationships("ppt/presentation.xml")
            return [(str(index + 1), rels[attr(node, "id")]) for index, node in enumerate(descendants(self.xml("ppt/presentation.xml"), "sldId")) if attr(node, "id") in rels]
        if suffix == ".xlsx":
            rels = self.relationships("xl/workbook.xml")
            return [(node.get("name", "Sheet"), rels[attr(node, "id")]) for node in descendants(self.xml("xl/workbook.xml"), "sheet") if attr(node, "id") in rels]
        root = self.xml("content.xml")
        name = "table" if suffix == ".ods" else "page" if suffix == ".odp" else None
        return [(attr(node, "name", str(index + 1)), index) for index, node in enumerate(descendants(root, name))] if name else [("Document", 0)]

    def word(self, part):
        root, rels = self.xml(part), self.relationships(part)
        body = next(descendants(root, "body"))
        blocks = []
        for node in body:
            if local(node) == "p":
                spans = []
                remaining = 12000
                for run in descendants(node, "r"):
                    value = "".join("\t" if local(item) == "tab" else "\n" if local(item) == "br" else item.text or ""
                        for item in run.iter() if local(item) in {"t", "tab", "br"})
                    if value and remaining > 0:
                        value = value[:min(TEXT_LIMIT, remaining)]
                        remaining -= len(value)
                        spans.append({"text": value, "bold": any(local(c) == "b" and attr(c, "val", "1") not in {"0", "false"} for c in run.iter()),
                            "italic": any(local(c) == "i" and attr(c, "val", "1") not in {"0", "false"} for c in run.iter())})
                style = next(descendants(node, "pStyle"), None)
                heading = attr(style, "val") if style is not None else ""
                blocks.append({"type": "paragraph", "spans": spans[:200], "heading": heading.lower().startswith(("heading", "title"))})
                for drawing in descendants(node, "blip"):
                    asset = rels.get(attr(drawing, "embed"))
                    if asset:
                        blocks.append({"type": "image", "asset": asset})
            elif local(node) == "tbl":
                rows = [["\n".join("".join(item.text or "" for item in descendants(p, "t")) for p in descendants(cell, "p"))[:TEXT_LIMIT]
                    for cell in row if local(cell) == "tc"][:100] for row in node if local(row) == "tr"]
                for start in range(0, min(len(rows), MAX_BLOCKS * 20), 20):
                    blocks.append({"type": "table", "rows": [[cell[:200] for cell in row[:50]] for row in rows[start:start + 20]]})
            if len(blocks) >= MAX_BLOCKS:
                break
        return blocks

    def slides(self, part):
        root, rels = self.xml(part), self.relationships(part)
        blocks = []
        for node in root.iter():
            if local(node) == "p":
                value = "".join(item.text or "" for item in descendants(node, "t"))[:TEXT_LIMIT]
                if value:
                    blocks.append({"type": "paragraph", "spans": [{"text": value}]})
            elif local(node) == "blip":
                asset = rels.get(attr(node, "embed"))
                if asset:
                    blocks.append({"type": "image", "asset": asset})
            if len(blocks) >= MAX_BLOCKS:
                break
        return blocks

    def spreadsheet(self, part, offset, limit):
        strings = []
        styles = []
        if "xl/styles.xml" in self.archive.namelist():
            formats = next(descendants(self.xml("xl/styles.xml"), "cellXfs"), [])
            styles = [int(item.get("numFmtId", "0")) for item in formats]
        props = next(descendants(self.xml("xl/workbook.xml"), "workbookPr"), None)
        date_1904 = props is not None and props.get("date1904") in {"1", "true"}
        if "xl/sharedStrings.xml" in self.archive.namelist():
            strings = ["".join(item.text or "" for item in descendants(node, "t"))[:TEXT_LIMIT] for node in self.xml("xl/sharedStrings.xml")]
        rows = list(descendants(self.xml(part), "row"))
        result, numbers = [], []
        for row in rows[offset:offset + limit]:
            values = {}
            for index, cell in enumerate(child for child in row if local(child) == "c"):
                letters = "".join(c for c in cell.get("r", "") if "A" <= c <= "Z")
                column = 0
                for c in letters:
                    column = column * 26 + ord(c) - 64
                column = column - 1 if letters else index
                if column >= 100:
                    continue
                value = next(descendants(cell, "v"), None)
                value = value.text or "" if value is not None else ""
                kind = cell.get("t")
                if kind == "s":
                    value = strings[int(value)] if value.isdigit() and int(value) < len(strings) else value
                elif kind == "inlineStr":
                    value = "".join(item.text or "" for item in descendants(cell, "t"))
                elif kind == "b":
                    value = "TRUE" if value == "1" else "FALSE"
                elif kind not in {"str", "e", "d"}:
                    value = cell_value(value, cell.get("s", "0"), styles, date_1904)
                formula = next(descendants(cell, "f"), None)
                if formula is not None and not value:
                    value = "=" + (formula.text or "")
                values[column] = value[:TEXT_LIMIT]
            result.append([values.get(i, "") for i in range(max(values, default=-1) + 1)])
            numbers.append(row.get("r", str(offset + len(result))))
        return {"kind": "table", "rows": result, "rowNumbers": numbers, "total": len(rows)}

    def odf(self, suffix, section):
        root = self.xml("content.xml")
        if suffix == ".odp":
            root = list(descendants(root, "page"))[section]
        elif suffix == ".odt":
            root = next(descendants(root, "text"))
        else:
            root = list(descendants(root, "table"))[section]
        blocks = []
        for node in root.iter():
            if local(node) in {"p", "h"} and not any(local(p) in {"table-cell", "covered-table-cell"} for p in node.iterancestors()):
                blocks.append({"type": "paragraph", "spans": [{"text": text(node)}], "heading": local(node) == "h"})
            elif local(node) == "image":
                blocks.append({"type": "image", "asset": attr(node, "href")})
            elif local(node) == "table-row":
                cells = []
                for cell in node:
                    repeat = min(100 - len(cells), int(attr(cell, "number-columns-repeated", "1")))
                    cells.extend([text(cell)] * repeat)
                repeat = min(MAX_BLOCKS - len(blocks), int(attr(node, "number-rows-repeated", "1")))
                blocks.extend({"type": "row", "values": cells} for _ in range(repeat))
            if len(blocks) >= MAX_BLOCKS:
                break
        return blocks


def content(package, suffix, part, section, offset, limit):
    if suffix == ".xlsx":
        return package.spreadsheet(part, offset, limit)
    if suffix == ".docx":
        blocks = package.word(part)
    elif suffix == ".pptx":
        blocks = package.slides(part)
    else:
        blocks = package.odf(suffix, section)
    if suffix == ".ods":
        rows = [b["values"] for b in blocks if b["type"] == "row"]
        return {"kind": "table", "rows": rows[offset:offset + limit], "total": len(rows)}
    if suffix in {".odt", ".odp"}:
        normalized = []
        for block in blocks:
            if block["type"] == "row":
                if normalized and normalized[-1]["type"] == "table" and len(normalized[-1]["rows"]) < 20:
                    normalized[-1]["rows"].append(block["values"])
                else:
                    normalized.append({"type": "table", "rows": [block["values"]]})
            else:
                normalized.append(block)
        blocks = normalized
    selected = blocks[offset:offset + limit]
    images = 0
    for block in selected:
        if block["type"] == "image":
            images += 1
            block["source"] = package.image(block.pop("asset")) if images <= 8 else None
    return {"kind": "document", "blocks": selected, "total": len(blocks)}
