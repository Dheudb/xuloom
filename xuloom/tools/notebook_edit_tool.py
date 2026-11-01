import json
import os
import uuid
from typing import Optional
from pydantic import BaseModel, Field
from xuloom.tools.base import BaseAgenticTool
from xuloom.context.tool_results import build_tool_result
from xuloom.features.file_changes import record_written_file, read_snapshot, check_observed_file, write_file_text


class NotebookEditToolInput(BaseModel):
    notebook_path: str = Field(
        ...,
        description="The absolute path to the Jupyter notebook file to edit (must be absolute, not relative).",
    )
    cell_id: Optional[str] = Field(
        None,
        description=(
            "The ID of the cell to edit. When inserting a new cell, the new cell will be inserted "
            "after the cell with this ID, or at the beginning if not specified."
        ),
    )
    new_source: str = Field(..., description="The new source for the cell.")
    cell_type: Optional[str] = Field(
        None,
        description=(
            "The type of the cell ('code' or 'markdown'). Defaults to the current cell type. "
            "Required when using edit_mode='insert'."
        ),
    )
    edit_mode: Optional[str] = Field(
        None,
        description="The type of edit to make: 'replace' (default), 'insert', or 'delete'.",
    )


class NotebookEditTool(BaseAgenticTool):
    workspace_paths = {"notebook_path": False}
    name = "notebook_edit_tool"
    description = "Replace, insert, or delete cells in a Jupyter notebook (.ipynb file)."
    input_schema = NotebookEditToolInput
    risk_level = "high"

    def prompt(self) -> str:
        return (
            "Completely replaces the contents of a specific cell in a Jupyter notebook (.ipynb file) "
            "with new source. The notebook_path parameter must be an absolute path, not a relative path. "
            "Use cell_id to identify the cell (0-indexed number or the cell's id string). "
            "Use edit_mode='insert' to add a new cell after the specified cell_id. "
            "Use edit_mode='delete' to delete the cell at the specified cell_id."
        )

    def _write_queue(self):
        return self.context.get("incremental_write_queue")

    def _cache(self):
        return self.context.get("file_state_cache")

    def validate_input(self, notebook_path: str, new_source: str, cell_id: str = None,
                       cell_type: str = None, edit_mode: str = None, **kwargs):
        if not notebook_path:
            return "notebook_path is required."
        if not notebook_path.endswith(".ipynb"):
            return "notebook_path must point to a .ipynb file."
        if edit_mode and edit_mode not in ("replace", "insert", "delete"):
            return f"edit_mode must be 'replace', 'insert', or 'delete', got '{edit_mode}'."
        if edit_mode == "insert" and not cell_type:
            return "cell_type is required when using edit_mode='insert'."
        if cell_type and cell_type not in ("code", "markdown"):
            return f"cell_type must be 'code' or 'markdown', got '{cell_type}'."
        return None

    def build_permission_summary(self, notebook_path: str, new_source: str, cell_id: str = None,
                                 cell_type: str = None, edit_mode: str = None, **kwargs) -> str:
        mode = edit_mode or "replace"
        preview = new_source[:160] + ("..." if len(new_source) > 160 else "")
        return (
            "Notebook edit requested.\n"
            f"path: {os.path.abspath(notebook_path)}\n"
            f"mode: {mode}\n"
            f"cell_id: {cell_id or '(beginning)'}\n"
            f"cell_type: {cell_type or '(current)'}\n"
            f"source_preview: {preview}"
        )

    @staticmethod
    def _parse_cell_index(cell_id_str: str) -> Optional[int]:
        if cell_id_str is None:
            return None
        try:
            return int(cell_id_str)
        except ValueError:
            pass
        if cell_id_str.startswith("cell-"):
            try:
                return int(cell_id_str[5:])
            except ValueError:
                pass
        return None

    async def execute(self, notebook_path: str, new_source: str, cell_id: str = None,
                      cell_type: str = None, edit_mode: str = None, **kwargs) -> str:
        abs_path = os.path.abspath(notebook_path)
        queue = self._write_queue()
        mutate = lambda: self._edit_notebook(abs_path, new_source, cell_id, cell_type, edit_mode)
        try:
            return await queue.run(abs_path, "notebook_edit", mutate) if queue is not None else mutate()
        except Exception as error:
            return build_tool_result(ok=False, content=f"Error editing notebook: {error}", is_error=True)

    def _edit_notebook(self, abs_path, new_source, cell_id, cell_type, edit_mode):
        mode = edit_mode or "replace"

        if not os.path.exists(abs_path):
            return build_tool_result(ok=False, content=f"Error: Notebook '{abs_path}' does not exist.", is_error=True)
        if not os.path.isfile(abs_path):
            return build_tool_result(ok=False, content=f"Error: '{abs_path}' is not a file.", is_error=True)

        cache = self._cache()
        has_read = bool(self.context.get("read_file_state", {}).get(abs_path, False)
                        or (cache is not None and cache.has_been_read(abs_path)))
        if not has_read:
            return build_tool_result(ok=False, content="Error: Notebook has not been read yet. Use file_read_tool to read it first.", is_error=True)

        try:
            check_observed_file(self.context, abs_path)
            with open(abs_path, "r", encoding="utf-8") as f:
                notebook = json.load(f)
        except Exception as e:
            return build_tool_result(ok=False, content=f"Error reading notebook: {e}", is_error=True)

        cells = notebook.get("cells", [])
        nbformat_major = notebook.get("nbformat", 4)
        nbformat_minor = notebook.get("nbformat_minor", 0)
        use_cell_ids = nbformat_major >= 5 or (nbformat_major == 4 and nbformat_minor >= 5)

        cell_index = None
        if cell_id is not None:
            for i, c in enumerate(cells):
                if c.get("id") == cell_id:
                    cell_index = i
                    break
            if cell_index is None:
                parsed = self._parse_cell_index(cell_id)
                if parsed is not None and 0 <= parsed < len(cells):
                    cell_index = parsed
                elif parsed is not None and parsed == len(cells) and mode == "replace":
                    mode = "insert"
                    if not cell_type:
                        cell_type = "code"
                    cell_index = len(cells)
                else:
                    return build_tool_result(ok=False, content=f"Error: Cell '{cell_id}' not found in notebook with {len(cells)} cells.", is_error=True)
        else:
            cell_index = 0

        if mode == "insert":
            if cell_id is not None and cell_index < len(cells):
                cell_index += 1

            new_cell = {
                "cell_type": cell_type or "code",
                "metadata": {},
                "source": new_source,
            }
            if (cell_type or "code") == "code":
                new_cell["execution_count"] = None
                new_cell["outputs"] = []
            if use_cell_ids:
                new_cell["id"] = uuid.uuid4().hex[:8]

            cells.insert(cell_index, new_cell)
            result_msg = f"Inserted new {new_cell['cell_type']} cell at index {cell_index}."

        elif mode == "delete":
            if cell_index >= len(cells):
                return build_tool_result(ok=False, content=f"Error: Cell index {cell_index} out of range (notebook has {len(cells)} cells).", is_error=True)
            deleted_type = cells[cell_index].get("cell_type", "unknown")
            cells.pop(cell_index)
            result_msg = f"Deleted {deleted_type} cell at index {cell_index}."

        else:
            if cell_index >= len(cells):
                return build_tool_result(ok=False, content=f"Error: Cell index {cell_index} out of range (notebook has {len(cells)} cells).", is_error=True)
            target = cells[cell_index]
            target["source"] = new_source
            if target.get("cell_type") == "code":
                target["execution_count"] = None
                target["outputs"] = []
            if cell_type and cell_type != target.get("cell_type"):
                target["cell_type"] = cell_type
                if cell_type == "code":
                    target.setdefault("execution_count", None)
                    target.setdefault("outputs", [])
            result_msg = f"Replaced cell {cell_index} ({target.get('cell_type', 'unknown')})."

        notebook["cells"] = cells

        def _perform_write():
            before_text = read_snapshot(abs_path)
            revision = write_file_text(abs_path, json.dumps(notebook, indent=1, ensure_ascii=False) + "\n", context=self.context)
            _, write_metadata = record_written_file(self.context, abs_path, before_text, "notebook_edit", revision=revision)
            if revision or write_metadata.get("post_write_warnings"):
                return build_tool_result(ok=True, content=result_msg, metadata=write_metadata)
            return result_msg

        try:
            return _perform_write()
        except Exception as e:
            return build_tool_result(ok=False, content=f"Error writing notebook: {e}", is_error=True)
