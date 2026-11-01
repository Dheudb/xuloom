import difflib
import os
import re
from pydantic import BaseModel, Field
from xuloom.tools.base import BaseAgenticTool
from xuloom.context.tool_results import build_tool_result
from xuloom.features.file_changes import record_written_file, read_snapshot, check_observed_file, write_file_text

class FileEditToolInput(BaseModel):
    file_path: str = Field(..., description="The absolute path to the file to modify")
    old_string: str = Field(..., description="The text to replace")
    new_string: str = Field(..., description="The text to replace it with (must be different from old_string)")
    replace_all: bool = Field(False, description="Replace all occurrences of old_string (default false)")

class FileEditTool(BaseAgenticTool):
    workspace_paths = {"file_path": False}
    name = "file_edit_tool"
    description = "Performs exact string replacements in files. The edit will FAIL if old_string is not unique in the file. You must use file_read_tool at least once before editing."
    input_schema = FileEditToolInput
    risk_level = "high"

    def _cache(self):
        return self.context.get("file_state_cache")

    def _artifact_tracker(self):
        return self.context.get("artifact_tracker")

    def _write_queue(self):
        return self.context.get("incremental_write_queue")

    def _normalize_text_for_fuzzy_match(self, text: str) -> str:
        normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        lines = []
        for line in normalized.split("\n"):
            compacted = re.sub(r"\s+", " ", line.strip())
            lines.append(compacted)
        return "\n".join(lines).strip()

    def _line_start_offsets(self, text: str):
        offsets = []
        cursor = 0
        for line in text.splitlines(keepends=True):
            offsets.append(cursor)
            cursor += len(line)
        if not offsets:
            offsets.append(0)
        return offsets

    def _find_fuzzy_line_match(self, content: str, target_content: str):
        target_lines_raw = target_content.splitlines(keepends=True)
        content_lines_raw = content.splitlines(keepends=True)
        if not target_lines_raw or not content_lines_raw:
            return None

        target_lines = [
            self._normalize_text_for_fuzzy_match(line)
            for line in target_lines_raw
        ]
        target_line_count = len(target_lines_raw)
        min_window = max(1, target_line_count - 2)
        max_window = min(len(content_lines_raw), target_line_count + 2)
        offsets = self._line_start_offsets(content)
        candidates = []

        for start in range(len(content_lines_raw)):
            for window_size in range(min_window, max_window + 1):
                end = start + window_size
                if end > len(content_lines_raw):
                    continue
                candidate_lines_raw = content_lines_raw[start:end]
                candidate_lines = [
                    self._normalize_text_for_fuzzy_match(line)
                    for line in candidate_lines_raw
                ]
                ratio = difflib.SequenceMatcher(
                    None,
                    "\n".join(target_lines),
                    "\n".join(candidate_lines),
                ).ratio()
                if ratio < 0.84:
                    continue
                start_offset = offsets[start]
                end_offset = offsets[end] if end < len(offsets) else len(content)
                candidates.append({
                    "ratio": ratio,
                    "start_line": start + 1,
                    "end_line": end,
                    "start_offset": start_offset,
                    "end_offset": end_offset,
                    "matched_text": content[start_offset:end_offset],
                })

        if not candidates:
            return None

        candidates.sort(key=lambda item: item["ratio"], reverse=True)
        best = candidates[0]
        close_competitors = [
            item for item in candidates
            if item["ratio"] >= max(0.84, best["ratio"] - 0.03)
        ]
        if best["ratio"] < 0.92:
            return None
        if len(close_competitors) > 1:
            unique_spans = {
                (item["start_offset"], item["end_offset"])
                for item in close_competitors
            }
            if len(unique_spans) > 1:
                return {
                    "ambiguous": True,
                    "candidate_count": len(unique_spans),
                    "best_ratio": best["ratio"],
                }

        return {
            "ambiguous": False,
            "candidate_count": len(close_competitors),
            **best,
        }

    def prompt(self) -> str:
        return (
            "You must use file_read_tool at least once in the conversation before editing. This tool will error if you attempt an edit without reading the file. "
            "When editing text from Read tool output, ensure you preserve the exact indentation (tabs/spaces) as it appears AFTER the line number prefix. "
            "The line number prefix format is: spaces + line number + pipe. Everything after that is the actual file content to match. Never include any part of the line number prefix in old_string or new_string. "
            "ALWAYS prefer editing existing files. NEVER write new files unless explicitly required. "
            "Only use emojis if the user explicitly requests it. "
            "The edit will FAIL if old_string is not unique in the file — provide more context to make it unique, or use replace_all. "
            "Use the smallest old_string that's clearly unique — usually 2-4 adjacent lines is sufficient. Avoid including 10+ lines of context when less uniquely identifies the target. "
            "Use replace_all for renaming strings across the file. "
            "Optional parameter: replace_all (boolean, default false) — if true, replaces all occurrences of old_string in the file."
        )

    def validate_input(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ):
        if not file_path:
            return "file_path is required."
        if old_string == new_string and old_string != "":
            return "old_string and new_string are identical; no edit would occur."
        return None

    def build_permission_summary(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> str:
        target_preview = old_string[:120] + ("..." if len(old_string) > 120 else "")
        replacement_preview = new_string[:120] + ("..." if len(new_string) > 120 else "")
        return (
            "File edit requested.\n"
            f"path: {os.path.abspath(file_path)}\n"
            f"replace_all: {replace_all}\n"
            f"old_string: {target_preview or '<new file>'}\n"
            f"new_string: {replacement_preview}"
        )
    
    async def execute(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> dict:
        abs_path = os.path.abspath(file_path)
        file_exists = os.path.exists(abs_path)
        cache = self._cache()
        tracker = self._artifact_tracker()
        agent_id = self.context.get("agent_id")
        session_id = self.context.get("session_id")
        
        # 1. THE BLIND-EDIT LOCK CHECK (Prior Read Required unless constructing new)
        has_read = bool(
            self.context.get("read_file_state", {}).get(abs_path, False)
            or (cache is not None and cache.has_been_read(abs_path))
        )
        
        if not has_read:
            if not file_exists and old_string == "":
                pass
            else:
                return build_tool_result(
                    ok=False,
                    content="File has not been read yet. You must use `file_read_tool` before attempting to write to it.",
                    metadata={"path": abs_path, "operation": "edit"},
                    is_error=True,
                )
        
        if old_string == "":
            if file_exists:
                return build_tool_result(
                    ok=False,
                    content=f"Cannot create new file because '{abs_path}' already exists.",
                    metadata={"path": abs_path, "operation": "create"},
                    is_error=True,
                )

            def _perform_create():
                if os.path.exists(abs_path):
                    raise ValueError("File was created while this edit waited. Read it before modifying; no write was performed.")
                os.makedirs(os.path.dirname(abs_path) or '.', exist_ok=True)
                revision = write_file_text(abs_path, new_string, create_only=True, context=self.context)
                created_snapshot, write_metadata = record_written_file(self.context, abs_path, "", "create",
                    replacement_preview=new_string, revision=revision)
                return build_tool_result(
                    ok=True,
                    content=f"Successfully created new file '{abs_path}'.",
                    metadata={
                        "path": abs_path,
                        "operation": "create",
                        "bytes_written": len(new_string.encode("utf-8")),
                        **write_metadata,
                        "sha256": (created_snapshot or {}).get("sha256"),
                        "size": (created_snapshot or {}).get("size"),
                        "mtime": (created_snapshot or {}).get("mtime"),
                    },
                )
            try:
                queue = self._write_queue()
                if queue is not None:
                    return await queue.run(abs_path, "create", _perform_create)
                return _perform_create()
            except Exception as e:
                return build_tool_result(
                    ok=False,
                    content=f"Error creating file: {str(e)}",
                    metadata={"path": abs_path, "operation": "create"},
                    is_error=True,
                )
                
        # 3. NORMAL EDIT SCENARIO
        if not file_exists:
            return build_tool_result(
                ok=False,
                content=f"File '{abs_path}' does not exist.",
                metadata={"path": abs_path, "operation": "edit"},
                is_error=True,
            )
            
        if not os.path.isfile(abs_path):
            return build_tool_result(
                ok=False,
                content=f"'{abs_path}' is a directory.",
                metadata={"path": abs_path, "operation": "edit"},
                is_error=True,
            )
            
        def _perform_edit():
            check_observed_file(self.context, abs_path)
            before_snapshot = cache.snapshot(abs_path) if cache is not None else None
            with open(abs_path, "r", encoding="utf-8") as file:
                content = file.read()

            match_strategy = "exact"
            match_ratio = 1.0
            fuzzy_meta = {}
            occurrences = content.count(old_string)

            if occurrences == 0:
                target_norm = old_string.replace('\\r\\n', '\\n')
                content_norm = content.replace('\\r\\n', '\\n')

                occurrences = content_norm.count(target_norm)
                if occurrences == 0:
                    if replace_all:
                        return build_tool_result(
                            ok=False,
                            content="old_string not found in the file. Fuzzy replacement is disabled when replace_all=True.",
                            metadata={"path": abs_path, "operation": "edit"},
                            is_error=True,
                        )

                    fuzzy_match = self._find_fuzzy_line_match(content, old_string)
                    if not fuzzy_match:
                        return build_tool_result(
                            ok=False,
                            content="old_string not found in the file. Exact, line-ending-normalized, and fuzzy patch matching all failed.",
                            metadata={"path": abs_path, "operation": "edit"},
                            is_error=True,
                        )
                    if fuzzy_match.get("ambiguous"):
                        return build_tool_result(
                            ok=False,
                            content=(
                                f"Fuzzy patch matching found {fuzzy_match.get('candidate_count')} plausible edit regions. "
                                "Provide more surrounding context to disambiguate the target block."
                            ),
                            metadata={
                                "path": abs_path,
                                "operation": "edit",
                                "match_strategy": "fuzzy_line_match",
                                "candidate_count": fuzzy_match.get("candidate_count"),
                                "best_ratio": fuzzy_match.get("best_ratio"),
                            },
                            is_error=True,
                        )

                    target_to_use = fuzzy_match["matched_text"]
                    content_to_use = content
                    repl_to_use = new_string
                    os_fallback_used = False
                    match_strategy = "fuzzy_line_match"
                    match_ratio = float(fuzzy_match.get("ratio", 0.0) or 0.0)
                    fuzzy_meta = {
                        "fuzzy_start_line": fuzzy_match.get("start_line"),
                        "fuzzy_end_line": fuzzy_match.get("end_line"),
                        "fuzzy_candidate_count": fuzzy_match.get("candidate_count"),
                    }
                    occurrences = 1
                else:
                    target_to_use = target_norm
                    content_to_use = content_norm
                    repl_to_use = new_string.replace('\\r\\n', '\\n')
                    os_fallback_used = True
                    match_strategy = "line_ending_normalized"
            else:
                target_to_use = old_string
                content_to_use = content
                repl_to_use = new_string
                os_fallback_used = False

            if occurrences > 1 and not replace_all:
                return build_tool_result(
                    ok=False,
                    content=f"old_string matched {occurrences} times. Set replace_all=True or provide more surrounding context.",
                    metadata={
                        "path": abs_path,
                        "operation": "edit",
                        "matches": occurrences,
                    },
                    is_error=True,
                )
                
            # Straight precise swap (either 1, or bulk all)
            new_content = content_to_use.replace(target_to_use, repl_to_use)
            revision = write_file_text(abs_path, new_content, context=self.context)
            after_snapshot, write_metadata = record_written_file(self.context, abs_path, content, "edit",
                before_snapshot=before_snapshot, target_preview=old_string, replacement_preview=new_string, revision=revision)

            msg = f"Successfully edited file '{abs_path}'."
            if replace_all and occurrences > 1:
                msg += f" (Replaced {occurrences} occurrences system-wide)."
            if os_fallback_used:
                msg += " (Whitespace/LF normalization fallback utilized)."
            if match_strategy == "fuzzy_line_match":
                msg += f" (Fuzzy patch match applied, similarity={match_ratio:.3f})."
            return build_tool_result(
                ok=True,
                content=msg,
                metadata={
                    "path": abs_path,
                    "operation": "edit",
                    "matches": occurrences,
                    "replace_all": replace_all,
                    "match_strategy": match_strategy,
                    "match_ratio": match_ratio,
                    "line_ending_normalized": os_fallback_used,
                    "bytes_written": len(new_content.encode("utf-8")),
                    **write_metadata,
                    "sha256_before": (before_snapshot or {}).get("sha256"),
                    "sha256_after": (after_snapshot or {}).get("sha256"),
                    "size_before": (before_snapshot or {}).get("size"),
                    "size_after": (after_snapshot or {}).get("size"),
                    "mtime_after": (after_snapshot or {}).get("mtime"),
                    **fuzzy_meta,
                },
            )

        try:
            queue = self._write_queue()
            if queue is not None:
                return await queue.run(abs_path, "edit", _perform_edit)
            return _perform_edit()
        except UnicodeDecodeError:
            return build_tool_result(
                ok=False,
                content=f"'{abs_path}' appears to be binary or has an unrecognized encoding.",
                metadata={"path": abs_path, "operation": "edit"},
                is_error=True,
            )
        except PermissionError:
            return build_tool_result(
                ok=False,
                content=f"Access denied to modify '{abs_path}'.",
                metadata={"path": abs_path, "operation": "edit"},
                is_error=True,
            )
        except Exception as e:
            return build_tool_result(
                ok=False,
                content=f"Error modifying file manually: {str(e)}",
                metadata={"path": abs_path, "operation": "edit"},
                is_error=True,
            )
