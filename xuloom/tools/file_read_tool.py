import os
import base64
from typing import Union, List, Dict, Any, Literal
from urllib.parse import urlsplit
from io import BytesIO
from pydantic import BaseModel, Field
from xuloom.tools.base import BaseAgenticTool

try:
    from PIL import Image
except ImportError:
    Image = None

class FileReadToolInput(BaseModel):
    file_path: str = Field(..., description="The absolute path of the file to read.")
    offset: int = Field(None, description="The line number to start reading from (1-indexed). Negative values count from end.")
    limit: int = Field(None, ge=1, description="Number of lines; defaults to 2000. Continue at the returned next line.")
    pages: str = Field(None, description="Page range for PDFs (e.g. '1-5'). Only used for PDF files.")
    force: bool = Field(False, description="Return original content again even when this range was previously read.")
    pdf_mode: Literal["auto", "text", "image"] = Field("auto", description="PDF: auto uses compatible native document transport or extracted text; image renders selected pages for figures/scans.")
    char_offset: int = Field(None, ge=0, description="Zero-based character offset for paging long text lines or archived tool output. Do not combine with line offset/limit.")
    char_limit: int = Field(3000, ge=1, le=4000, description="Maximum characters for a char_offset page.")

class FileReadTool(BaseAgenticTool):
    workspace_paths = {"file_path": False}
    name = "file_read_tool"
    description = "Read an authorized local file. Access remains subject to tool permissions and the current system execution isolation policy."
    input_schema = FileReadToolInput
    is_read_only = True
    risk_level = "low"

    def prepare_input(self, values):
        if isinstance(values.get("file_path"), str) and values["file_path"].startswith(("http://", "https://")):
            from xuloom.protocols.dsml import ToolProtocolError
            raise ToolProtocolError('This is a webpage URL, not a local file. Use web_fetch_tool with {"url": "the URL"}; no file was read and no network request was made.')
        return super().prepare_input(values)

    def prompt(self) -> str:
        return """Read authorized files; use command_tool to list directories.
Text defaults to 2000 lines; negative offsets count from the end. Follow the next-line cursor, or use char_offset for long lines. force=true re-delivers evidence after compaction.
PDF pages accepts a single page (2), closed range (2-5), or open range (2-). pdf_mode=image renders at most 10 selected pages for figures/scans and requires image input. Extracted text is not proof that figures were read. Missing text and truncation are explicit.
Images are delivered visually. Notebook JSON is read as text; discover notebook tools for cell operations."""

    def _cache(self):
        return self.context.get("file_state_cache")

    def _artifact_tracker(self):
        return self.context.get("artifact_tracker")

    def _inputs(self):
        from xuloom.core.model_inputs import configured_inputs
        model = self.context.get("request_model", self.context.get("primary_model", ""))
        contract = self.context.get("model_input_contract")
        return (self.context["model_input_modalities"] if "model_input_modalities" in self.context
                else contract(model) if callable(contract) else configured_inputs(
                    self.context.get("model_provider", ""), model, self.context.get("api_base_url", "")))

    def _is_native_anthropic(self) -> bool:
        base = urlsplit(self.context.get("api_base_url") or "https://api.anthropic.com")
        return (self.context.get("model_provider") == "anthropic" and base.scheme == "https"
                and base.netloc.lower() == "api.anthropic.com" and "document" in (self._inputs() or []))

    def _pdf(self, path, pages, offset, limit, mode):
        try:
            import pymupdf
        except ImportError:
            raise ValueError('PDF reading needs the optional PDF support: pip install "xuloom[pdf]" '
                             "(PyMuPDF, AGPL-3.0 licensed)") from None
        if mode not in {"auto", "text", "image"}:
            raise ValueError("pdf_mode must be auto, text or image")
        if pages and (offset is not None or limit is not None):
            raise ValueError("Use pages or offset/limit, not both")
        with pymupdf.open(path) as doc:
            total = len(doc)
            if pages:
                import re
                match = re.fullmatch(r"([1-9][0-9]*)(?:-([1-9][0-9]*)?)?", pages.strip())
                if not match: raise ValueError("Use a page number, closed range or open range, e.g. 2, 2-5, 2-")
                start = int(match[1])
                end = int(match[2]) if match[2] else total if "-" in pages else start
            else:
                start = offset if offset is not None else 1
                if start < 0: start = total + start + 1
                end = start + limit - 1 if limit is not None else total
            if not 1 <= start <= end <= total:
                raise ValueError(f"PDF page range must be within 1..{total}")
            heading = f"PDF: {path}\nPages: {start} to {end} of {total}\n"
            if mode == "image":
                from xuloom.core.model_inputs import validate_media
                validate_media([{"type":"image"}], self._inputs())
                if end-start+1 > 10: raise ValueError("Render at most 10 pages per image request")
                result = [{"type":"text", "text":heading + "Rendered pages; no OCR or scientific verification performed."}]
                for number in range(start-1,end):
                    page = doc[number]
                    scale = min(2, 1500 / max(page.rect.width, page.rect.height))
                    png = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).tobytes("png")
                    result.extend([{"type":"text", "text":f"Page {number+1}"},
                        {"type":"image", "source":{"type":"base64", "media_type":"image/png", "data":base64.b64encode(png).decode()}}])
                return result
            if mode == "auto" and self._is_native_anthropic():
                with pymupdf.open() as selected:
                    selected.insert_pdf(doc, from_page=start-1, to_page=end-1)
                    data = base64.b64encode(selected.tobytes()).decode()
                return [{"type":"text", "text":heading}, {"type":"document", "source":{"type":"base64", "media_type":"application/pdf", "data":data}}]
            output = heading + "Text extraction only; figures/layout are not delivered. Use pdf_mode=image for selected pages.\n"
            for number in range(start-1,end):
                text = doc[number].get_text("text")
                if not text.strip(): text = "[No extractable text: this page has not been read. Use pdf_mode=image with a vision-capable model.]"
                segment = f"--- Page {number+1} ---\n{text}\n"
                if len(output) + len(segment) > 120000:
                    return output + f"[Truncated before page {number+1}; remaining pages were not delivered. Read a smaller page range or pdf_mode=image.]"
                output += segment
            return output

    async def execute(self, file_path: str, offset: int = None, limit: int = None, pages: str = None, char_offset: int = None, char_limit: int = 3000, force: bool = False, pdf_mode: str = "auto", **kwargs) -> Union[str, List[Dict[str, Any]]]:
        abs_path = os.path.abspath(file_path)
        cache = self._cache()
        tracker = self._artifact_tracker()
        agent_id = self.context.get("agent_id")
        session_id = self.context.get("session_id")

        if not os.path.exists(abs_path):
            return f"Error: File at '{abs_path}' does not exist."

        if not os.path.isfile(abs_path):
            return f"Error: '{abs_path}' is a directory, not a file."

        ext = os.path.splitext(abs_path)[1].lower()

        # 1. Image parsing (Multimodal vision)
        if ext in ['.png', '.jpg', '.jpeg', '.webp', '.gif']:
            from xuloom.core.model_inputs import configured_inputs, validate_media, UnsupportedInput
            try:
                model = self.context.get("request_model", self.context.get("primary_model", ""))
                contract = self.context.get("model_input_contract")
                inputs = self.context["model_input_modalities"] if "model_input_modalities" in self.context else contract(model) if callable(contract) else configured_inputs(self.context.get("model_provider", ""), model, self.context.get("api_base_url", ""))
                validate_media([{"type": "image"}], inputs)
            except UnsupportedInput as error:
                return "Error: " + str(error)
            if Image is None:
                return "Error: Image reading requires the 'pillow' library to be installed."

            try:
                img = Image.open(abs_path)
                if ext in ['.jpg', '.jpeg'] and img.mode != 'RGB':
                    img = img.convert('RGB')

                max_dim = 1500
                if img.width > max_dim or img.height > max_dim:
                    img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)

                output_buf = BytesIO()
                media_type = f"image/{'jpeg' if ext in ['.jpg', '.jpeg'] else ext[1:]}"

                fmt = "JPEG" if ext in ['.jpg', '.jpeg'] else ext[1:].upper()
                img.save(output_buf, format=fmt)
                img_bytes = output_buf.getvalue()
                b64_data = base64.b64encode(img_bytes).decode('utf-8')

                if cache is not None:
                    cache.record_read(abs_path, kind="image", chars=len(img_bytes))
                self.context.setdefault("read_file_state", {})[abs_path] = True
                if tracker is not None:
                    tracker.record_prefetch(
                        path=abs_path, kind="image", source="file_read_tool",
                        start_line=None, end_line=None,
                        chars=len(img_bytes), agent_id=agent_id, session_id=session_id,
                    )
                    tracker.record_attachment(
                        path=abs_path, kind="image", source="file_read_tool",
                        agent_id=agent_id, session_id=session_id,
                        metadata={"bytes": len(img_bytes), "media_type": media_type},
                    )

                return [
                    {"type": "text", "text": f"Successfully read image '{abs_path}'. Size: {img.width}x{img.height}"},
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64_data}}
                ]
            except Exception as e:
                return f"Error opening image: {str(e)}"

        # PDF output is always re-delivered: text extraction and page images are
        # different evidence, and an earlier delivery may have been compacted.
        if ext == '.pdf':
            try:
                if char_offset is not None: raise ValueError("Use pages for PDF input")
                result = self._pdf(abs_path, pages, offset, limit, pdf_mode)
                if tracker is not None:
                    # This records delivery, not a claim of full-document reading.
                    heading = result[0]["text"] if isinstance(result, list) else result.split("\n", 2)[0:2]
                    tracker.record_attachment(path=abs_path, kind="pdf", source="file_read_tool",
                        agent_id=agent_id, session_id=session_id,
                        metadata={"requested_pages": pages, "offset": offset, "limit": limit,
                            "delivery_mode": "native" if isinstance(result, list) and any(b["type"]=="document" for b in result) else "image" if isinstance(result, list) else "text",
                            "page_heading": heading, "full_document_read_confirmed": False})
                return result
            except Exception as error:
                return f"Error loading PDF: {error}"

        # 3. Text fallback
        try:
            if char_offset is not None:
                from xuloom.context.tool_results import build_tool_result
                if char_offset < 0 or not 1 <= char_limit <= 4000 or offset is not None or limit is not None:
                    return build_tool_result(ok=False, is_error=True, content="Use char_offset >= 0 and char_limit from 1 to 4000 without line offset/limit.")
                with open(abs_path, "r", encoding="utf-8", newline="") as file:
                    remaining = char_offset
                    while remaining:
                        skipped = file.read(min(remaining, 65536))
                        if not skipped:
                            break
                        remaining -= len(skipped)
                    text = file.read(char_limit)
                    more = bool(file.read(1))
                return build_tool_result(ok=True, content=text, metadata={"path": abs_path, "char_offset": char_offset,
                    "chars_returned": len(text), "next_char_offset": char_offset + len(text) if more else None,
                    "truncated": more, "eof": not more})
            if offset == 0 or limit is not None and limit < 1:
                return "Error: offset is 1-based (or negative from end); limit must be positive."
            maximum = limit if limit is not None else 2000
            start_line = offset if offset is not None else 1
            from xuloom.context.file_state_cache import FileStateCache
            snapshot,_start,content_slice,more=(cache or FileStateCache()).text_page(abs_path,start_line,maximum)
            _end=_start+len(content_slice)-1
            entry=cache.get_entry(abs_path) if cache else None
            if not force and entry and entry.get('sha256')==snapshot['sha256'] and cache._range_key(_start,_end) in entry.get('ranges',{}):
                return f"File '{abs_path}' is unchanged for this range. Skipping duplicate inline content; use force=true to read the original evidence again."
            output = f"File: {abs_path}\nLines: {_start} to {_end}\n" if content_slice else f"File: {abs_path}\nFile is empty.\n"
            output += "\n".join(f"{_start+i}: {line.rstrip(chr(10))}" for i,line in enumerate(content_slice))
            output += f"\n[More content: next offset={_end+1}]" if more else "\n[End of file]"

            if cache is not None:
                cache.record_read(abs_path, kind="text", start_line=_start, end_line=_end, chars=len(output),_snapshot=snapshot)
            self.context.setdefault("read_file_state", {})[abs_path] = True
            if tracker is not None:
                tracker.record_prefetch(
                    path=abs_path, kind="text", source="file_read_tool",
                    start_line=_start, end_line=_end,
                    chars=len(output), agent_id=agent_id, session_id=session_id,
                )

            return output

        except UnicodeDecodeError:
            return f"Error: '{abs_path}' is binary or unrecognized. Text decode failed."
        except Exception as e:
            return f"Error reading file: {str(e)}"
