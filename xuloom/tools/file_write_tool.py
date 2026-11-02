import os
from pydantic import BaseModel, Field

from xuloom.context.tool_results import build_tool_result
from xuloom.tools.base import BaseAgenticTool
from xuloom.features.file_changes import record_written_file, read_snapshot, check_observed_file, write_file_text


class FileWriteToolInput(BaseModel):
    file_path: str = Field(..., description="The absolute path of the file to write.")
    content: str = Field(..., description="The content to write to the file.")


class FileWriteTool(BaseAgenticTool):
    workspace_paths = {"file_path": False}
    name = "file_write_tool"
    description = "Writes a file to the local filesystem. Overwrites the existing file if one exists at the path."
    input_schema = FileWriteToolInput
    risk_level = "high"

    def _cache(self):
        return self.context.get("file_state_cache")


    def _write_queue(self):
        return self.context.get("incremental_write_queue")

    def prompt(self) -> str:
        return (
            "This tool will overwrite the existing file if there is one at the provided path. "
            "If this is an existing file, you MUST use file_read_tool first to read the file's contents. "
            "Prefer file_edit_tool for modifying existing files — it only sends the diff. "
            "Only use this tool to create new files or for complete rewrites. "
        )

    def validate_input(self, file_path: str, content: str, **kwargs):
        if not file_path:
            return "file_path is required."
        return None

    def build_permission_summary(self, file_path: str, content: str, **kwargs) -> str:
        content_preview = content[:160] + ("..." if len(content) > 160 else "")
        return (
            "File write requested.\n"
            f"path: {os.path.abspath(file_path)}\n"
            f"content_preview: {content_preview}"
        )

    async def execute(self, file_path: str, content: str, **kwargs) -> dict:
        abs_path = os.path.abspath(file_path)
        file_exists = os.path.exists(abs_path)
        cache = self._cache()
        has_read = bool(
            self.context.get("read_file_state", {}).get(abs_path, False)
            or (cache is not None and cache.has_been_read(abs_path))
        )

        if file_exists and not os.path.isfile(abs_path):
            return build_tool_result(
                ok=False,
                content=f"'{abs_path}' is a directory.",
                metadata={"path": abs_path, "operation": "write"},
                is_error=True,
            )

        operation = "create"
        before_snapshot = None
        if file_exists:
            if not has_read:
                return build_tool_result(
                    ok=False,
                    content="File has not been read yet. Use `file_read_tool` before overwriting it.",
                    metadata={"path": abs_path, "operation": "overwrite"},
                    is_error=True,
                )
            operation = "overwrite"
            before_snapshot = cache.snapshot(abs_path) if cache is not None else None

        def _perform_write():
            exists_now = os.path.exists(abs_path)
            if not file_exists and exists_now:
                raise ValueError("File was created by another operation while waiting. Read it before overwriting; no write was performed.")
            if file_exists:
                check_observed_file(self.context, abs_path)
            before_text = read_snapshot(abs_path) if os.path.isfile(abs_path) else ""
            os.makedirs(os.path.dirname(abs_path) or ".", exist_ok=True)
            revision = write_file_text(abs_path, content, create_only=not file_exists, context=self.context)
            after_snapshot, write_metadata = record_written_file(self.context, abs_path, before_text, operation,
                before_snapshot=before_snapshot, replacement_preview=content, revision=revision)
            success_verb = "created" if operation == "create" else "overwrote"
            return build_tool_result(
                ok=True,
                content=f"Successfully {success_verb} file '{abs_path}'.",
                metadata={
                    "path": abs_path,
                    "operation": operation,
                    "bytes_written": len(content.encode("utf-8")),
                    **write_metadata,
                    "sha256_before": (before_snapshot or {}).get("sha256"),
                    "sha256_after": (after_snapshot or {}).get("sha256"),
                    "size_before": (before_snapshot or {}).get("size"),
                    "size_after": (after_snapshot or {}).get("size"),
                    "mtime_after": (after_snapshot or {}).get("mtime"),
                },
            )
        queue = self._write_queue()
        try:
            if queue is not None:
                return await queue.run(abs_path, operation, _perform_write)
            return _perform_write()
        except PermissionError:
            return build_tool_result(
                ok=False,
                content=f"Access denied to write '{abs_path}'.",
                metadata={"path": abs_path, "operation": operation},
                is_error=True,
            )
        except Exception as e:
            return build_tool_result(
                ok=False,
                content=f"Error writing file: {str(e)}",
                metadata={"path": abs_path, "operation": operation},
                is_error=True,
            )
