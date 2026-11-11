import subprocess
import os
import sys
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field
from xuloom.tools.base import BaseAgenticTool

class GrepToolInput(BaseModel):
    pattern: str = Field(..., description="The regular expression pattern to search for in file contents.")
    path: Optional[str] = Field(None, description="File or directory to search in. Defaults to current working directory.")
    glob: Optional[str] = Field(None, description="Glob pattern to filter files (e.g. '*.js', '*.{ts,tsx}').")
    output_mode: Optional[str] = Field(
        None,
        description=(
            "Output mode: 'content' shows matching lines with context (default), "
            "'files_with_matches' shows only file paths, "
            "'count' shows match counts per file."
        ),
    )
    before_context: Optional[int] = Field(None, alias="-B", description="Number of lines to show before each match (rg -B).")
    after_context: Optional[int] = Field(None, alias="-A", description="Number of lines to show after each match (rg -A).")
    context: Optional[int] = Field(None, alias="-C", description="Number of lines to show before and after each match (rg -C).")
    case_insensitive: Optional[bool] = Field(None, alias="-i", description="Case insensitive search (rg -i). Defaults to false.")
    type: Optional[str] = Field(None, description="File type to search (rg --type). Common types: js, py, rust, go, java, etc.")
    head_limit: Optional[int] = Field(None, description="Limit number of results shown.")
    offset: Optional[int] = Field(None, description="Skip first N results for pagination.")
    multiline: Optional[bool] = Field(None, description="Enable multiline mode where . matches newlines and patterns can span lines (rg -U --multiline-dotall).")

    class Config:
        populate_by_name = True


class GrepTool(BaseAgenticTool):
    workspace_paths = {"path": True}
    name = "grep_tool"
    description = (
        "A powerful search tool built on ripgrep. "
        "Supports full regex syntax, file type filters, context lines, and multiple output modes. "
        "ALWAYS use grep_tool for search tasks. NEVER invoke grep or rg as a bash_tool command."
    )
    input_schema = GrepToolInput
    is_read_only = True
    risk_level = "low"

    def prompt(self) -> str:
        return """Usage:
- Prefer using grep_tool for search tasks when you know the exact symbols or strings to search for.
- Supports full regex syntax (e.g., "log.*Error", "function\\s+\\w+")
- Filter files with glob parameter (e.g., "*.js", "**/*.tsx") or type parameter (e.g., "js", "py", "rust")
- Output modes: "content" shows matching lines (default), "files_with_matches" shows only file paths, "count" shows match counts
- Pattern syntax: Uses ripgrep (not grep) — literal braces need escaping (use interface\\{\\} to find interface{} in Go code)
- Multiline matching: By default patterns match within single lines only. For cross-line patterns like struct \\{[\\s\\S]*?field, use multiline: true
- Results are capped for responsiveness; when truncation occurs, refine your search pattern.
- Content output uses -B (before), -A (after), -C (before+after) for context lines around matches."""

    async def execute(
        self,
        pattern: str,
        path: str = None,
        glob: str = None,
        output_mode: str = None,
        before_context: int = None,
        after_context: int = None,
        context: int = None,
        case_insensitive: bool = None,
        type: str = None,
        head_limit: int = None,
        offset: int = None,
        multiline: bool = None,
        **kwargs,
    ) -> str:
        before_context = before_context or kwargs.get("-B")
        after_context = after_context or kwargs.get("-A")
        context = context or kwargs.get("-C")
        case_insensitive = case_insensitive if case_insensitive is not None else kwargs.get("-i")

        search_path = path or os.getcwd()
        search_path = os.path.abspath(search_path)

        # Desktop distributions carry this verified binary; library installs retain
        # their system ripgrep. Never search the current workspace for executables.
        bundled = Path(sys.prefix) / "Lib/site-packages/xuloom_search/rg.exe"
        executable = str(bundled) if bundled.is_file() else "rg"
        cmd = [executable, "-n", "--max-columns", "500"]

        if output_mode == "files_with_matches":
            cmd.append("-l")
        elif output_mode == "count":
            cmd.append("-c")

        if case_insensitive:
            cmd.append("-i")

        if multiline:
            cmd.extend(["-U", "--multiline-dotall"])

        if context is not None:
            cmd.extend(["-C", str(context)])
        else:
            if before_context is not None:
                cmd.extend(["-B", str(before_context)])
            if after_context is not None:
                cmd.extend(["-A", str(after_context)])

        if type:
            cmd.extend(["--type", type])

        if glob:
            cmd.extend(["-g", glob])

        cmd.extend([pattern, search_path])

        try:
            if self.context.get("confined_sync_worker"):
                process = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=40,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                stdout, stderr = process.stdout, process.stderr
            else:
                import asyncio
                process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                try:
                    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=40)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                    raise TimeoutError("Grep timed out") from None

            if process.returncode == 0:
                output = stdout.decode("utf-8", errors="replace")
                lines = output.splitlines()

                if output_mode in ("files_with_matches", "count"):
                    effective_offset = offset or 0
                    effective_limit = head_limit or 500
                    if effective_offset > 0:
                        lines = lines[effective_offset:]
                    if len(lines) > effective_limit:
                        visible = lines[:effective_limit]
                        next_off = effective_offset + effective_limit
                        return "\n".join(visible) + f"\n\n... [Showing {effective_limit} items. Next offset: {next_off}]"
                    return "\n".join(lines) if lines else "No matches found."

                mtimes = {}
                for line in lines:
                    if ":" in line:
                        f_name = line.split(":", 1)[0]
                        if f_name not in mtimes:
                            try:
                                mtimes[f_name] = os.path.getmtime(f_name)
                            except Exception:
                                mtimes[f_name] = 0

                indexed_lines = list(enumerate(lines))

                def sort_key(item):
                    idx, line = item
                    if ":" not in line:
                        return (0, line, idx)
                    f_name = line.split(":", 1)[0]
                    return (-mtimes.get(f_name, 0), f_name, idx)

                indexed_lines.sort(key=sort_key)
                sorted_lines = [item[1] for item in indexed_lines]

                effective_offset = offset or 0
                if effective_offset > 0:
                    sorted_lines = sorted_lines[effective_offset:]

                effective_limit = head_limit or 500
                if len(sorted_lines) > effective_limit:
                    visible_block = sorted_lines[:effective_limit]
                    next_offset = effective_offset + effective_limit
                    return (
                        "\n".join(visible_block)
                        + f"\n\n... [Pagination applied. Showing {effective_limit} items. Next offset: {next_offset}. "
                        f"Supply offset={next_offset} to view more matches.]"
                    )

                if effective_offset > 0 and len(sorted_lines) == 0:
                    return "No more pagination matches at this offset."

                return "\n".join(sorted_lines)

            elif process.returncode == 1:
                return "No matches found."
            else:
                return f"Ripgrep error (exit {process.returncode}): {stderr.decode('utf-8', errors='replace')}"

        except (TimeoutError, subprocess.TimeoutExpired):
            return "Error: Grep search timed out after 40 seconds. Try a more specific pattern or narrower path."
        except FileNotFoundError:
            return "Error: 'ripgrep' (rg) is not installed. Please install ripgrep for fast codebase search."
        except Exception as e:
            return f"Search execution error: {str(e)}"
