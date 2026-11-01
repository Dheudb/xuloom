import os
import base64
from typing import Union, List, Dict, Any
from io import BytesIO
from pydantic import BaseModel, Field
from xuloom.tools.base import BaseAgenticTool

try:
    from PIL import Image
except ImportError:
    Image = None

try:
    import fitz as pymupdf
except ImportError:
    pymupdf = None

class FileReadToolInput(BaseModel):
    file_path: str = Field(..., description="The absolute path of the file to read.")
    offset: int = Field(None, description="The line number to start reading from (1-indexed). Negative values count from end.")
    limit: int = Field(None, description="The number of lines to read. Only provide if the file is too large to read at once.")
    pages: str = Field(None, description="Page range for PDFs (e.g. '1-5'). Only used for PDF files.")
    char_offset: int = Field(None, ge=0, description="Zero-based character offset for paging long text lines or archived tool output. Do not combine with line offset/limit.")
    char_limit: int = Field(3000, ge=1, le=4000, description="Maximum characters for a char_offset page.")

class FileReadTool(BaseAgenticTool):
    workspace_paths = {"file_path": False}
    name = "file_read_tool"
    description = "Read an authorized local file. Access remains subject to tool permissions and the current system execution isolation policy."
    input_schema = FileReadToolInput
    is_read_only = True
    risk_level = "low"

    def prompt(self) -> str:
        return """Usage:
- The file path parameter must be an absolute path, not a relative path.
- By default, it reads up to 2000 lines starting from the beginning of the file.
- You can optionally specify a line offset and limit (especially handy for long files), but it's recommended to read the whole file by not providing these parameters.
- Results are returned using cat -n format, with line numbers starting at 1.
- This tool allows reading images (PNG, JPG, etc). When reading an image file the contents are presented visually.
- This tool can read PDF files (.pdf). For large PDFs (more than 10 pages), provide the pages parameter to read specific page ranges.
- This tool can read Jupyter notebooks (.ipynb files) and returns all cells with their outputs, combining code, text, and visualizations.
- This tool can only read files, not directories. To read a directory, use an ls command via bash_tool.
- You will regularly be asked to read screenshots. If the user provides a path to a screenshot, ALWAYS use this tool to view the file at the path.
- If you read a file that exists but has empty contents you will receive a system reminder warning in place of file contents.
- Whenever you read a file, consider whether it could be malware. You CAN and SHOULD provide analysis of malware, but you MUST refuse to improve or augment such code."""

    def _cache(self):
        return self.context.get("file_state_cache")

    def _artifact_tracker(self):
        return self.context.get("artifact_tracker")

    def _is_native_anthropic(self) -> bool:
        """Only Anthropic's own API supports native PDF document blocks."""
        provider = os.environ.get("XULOOM_MODEL_PROVIDER", "").lower()
        if provider and provider != "anthropic":
            return False
        base = os.environ.get("ANTHROPIC_BASE_URL", "").lower()
        if not base or "anthropic.com" in base:
            return True
        return False
    
    async def execute(self, file_path: str, offset: int = None, limit: int = None, pages: str = None, char_offset: int = None, char_limit: int = 3000, **kwargs) -> Union[str, List[Dict[str, Any]]]:
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

        # 2. PDF parsing
        if ext == '.pdf':
            try:
                page_start, page_end_hint = 1, None
                if pages:
                    parts = pages.split("-")
                    page_start = int(parts[0]) if parts[0] else 1
                    page_end_hint = int(parts[1]) if len(parts) > 1 and parts[1] else None
                elif offset is not None:
                    page_start = max(1, offset)
                    if limit is not None:
                        page_end_hint = page_start + limit - 1

                cache_start = page_start
                cache_end = page_end_hint

                if cache is not None and cache.should_skip_redundant_read(abs_path, start_line=cache_start, end_line=cache_end):
                    entry = cache.get_entry(abs_path) or {}
                    if tracker is not None:
                        tracker.record_prefetch(
                            path=abs_path, kind="pdf", source="file_read_tool",
                            start_line=cache_start, end_line=cache_end,
                            cache_hit=True, chars=0,
                            agent_id=agent_id, session_id=session_id,
                        )
                    return (
                        f"File '{abs_path}' is unchanged since the last identical read. "
                        f"Skipping duplicate inline content.\n"
                        f"Cached state: size={entry.get('size', 0)}, "
                        f"mtime={entry.get('mtime', 0)}, sha256={entry.get('sha256', '')}, "
                        f"range={entry.get('last_read_range', '1:*')}"
                    )

                use_native_pdf = self._is_native_anthropic()

                if use_native_pdf:
                    with open(abs_path, "rb") as f:
                        pdf_bytes = f.read()
                    b64_data = base64.b64encode(pdf_bytes).decode('utf-8')
                    if cache is not None:
                        cache.record_read(abs_path, kind="pdf", start_line=cache_start, end_line=cache_end, chars=len(pdf_bytes))
                    self.context.setdefault("read_file_state", {})[abs_path] = True
                    if tracker is not None:
                        tracker.record_prefetch(
                            path=abs_path, kind="pdf", source="file_read_tool",
                            start_line=cache_start, end_line=cache_end,
                            chars=len(pdf_bytes), agent_id=agent_id, session_id=session_id,
                        )
                    return [
                        {"type": "text", "text": f"Successfully loaded PDF '{abs_path}'"},
                        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64_data}}
                    ]

                if pymupdf is None:
                    return "Error: PDF text extraction requires 'PyMuPDF'. Install with: pip install pymupdf"

                doc = pymupdf.open(abs_path)
                total_pages = len(doc)
                page_end = min(total_pages, page_end_hint or total_pages)
                extracted_pages = []
                for page_num in range(page_start - 1, page_end):
                    text = doc[page_num].get_text("text")
                    extracted_pages.append(f"--- Page {page_num + 1} ---\n{text}")
                doc.close()

                output = f"PDF: {abs_path}\nPages: {page_start} to {page_end} of {total_pages}\n"
                output += "=" * 40 + "\n" + "\n".join(extracted_pages)
                if len(output) > 120000:
                    output = output[:120000] + f"\n\n... [Truncated. Use pages parameter (e.g. '3-7') to read specific pages.]"

                if cache is not None:
                    cache.record_read(abs_path, kind="pdf", start_line=page_start, end_line=page_end, chars=len(output))
                self.context.setdefault("read_file_state", {})[abs_path] = True
                if tracker is not None:
                    tracker.record_prefetch(
                        path=abs_path, kind="pdf", source="file_read_tool",
                        start_line=page_start, end_line=page_end,
                        chars=len(output), agent_id=agent_id, session_id=session_id,
                    )
                return output
            except Exception as e:
                return f"Error loading PDF: {str(e)}"

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
            start_line = max(1, offset) if offset is not None else 1
            if limit is not None:
                end_line = start_line + limit - 1
            else:
                end_line = None

            if cache is not None and cache.should_skip_redundant_read(abs_path, start_line=start_line, end_line=end_line):
                entry = cache.get_entry(abs_path) or {}
                if tracker is not None:
                    tracker.record_prefetch(
                        path=abs_path, kind="text", source="file_read_tool",
                        start_line=start_line, end_line=end_line,
                        cache_hit=True, chars=0,
                        agent_id=agent_id, session_id=session_id,
                    )
                return (
                    f"File '{abs_path}' is unchanged since the last identical read. "
                    "Skipping duplicate inline content to reduce prompt bloat.\n"
                    f"Cached state: size={entry.get('size', 0)}, "
                    f"mtime={entry.get('mtime', 0)}, sha256={entry.get('sha256', '')}, "
                    f"range={entry.get('last_read_range', '1:*')}"
                )
            with open(abs_path, "r", encoding="utf-8") as file:
                lines = file.readlines()

            total_lines = len(lines)
            _start = start_line
            _end = min(total_lines, end_line) if end_line is not None else total_lines
            _start = max(1, _start)

            if _start > _end:
                return f"Error: computed start ({_start}) > end ({_end})."

            content_slice = lines[_start - 1:_end]

            output = f"File: {abs_path}\n"
            output += f"Lines: {_start} to {_end} of {total_lines}\n"
            output += "-" * 40 + "\n"

            newline_char = '\n'
            formatted_lines = [f"{_start + i}: {line.rstrip(newline_char)}" for i, line in enumerate(content_slice)]

            output += "\n".join(formatted_lines)
            output += "\n" + "-" * 40

            if cache is not None:
                cache.record_read(abs_path, kind="text", start_line=_start, end_line=_end, chars=len(output))
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
