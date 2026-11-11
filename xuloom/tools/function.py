"""Turn a typed Python function into a tool.

    from xuloom import tool

    @tool(read_only=True)
    async def lookup_ticket(ticket_id: str, include_history: bool = False) -> dict:
        \"\"\"Return the status of an internal ticket.

        Args:
            ticket_id: Ticket identifier such as "T-123".
            include_history: Also return past status changes.
        \"\"\"
        return {"id": ticket_id, "status": "open"}

    config = EngineConfig(..., extra_tools=(lookup_ticket,))   # or api.add_tool(lookup_ticket) in a plugin

The decorator returns a :class:`BaseAgenticTool` *subclass*; every engine that registers it
creates its own instance, so one decorated function can serve many engines. The input
schema comes from the type hints (anything Pydantic accepts, including ``Literal``,
``list[str]`` and ``Annotated[int, Field(ge=1)]``), the description from the docstring's
first paragraph, and per-argument descriptions from a Google-style ``Args:`` section.

Sync functions run in a worker thread. A parameter named ``context`` receives the tool
context (``session_cwd``, ``session_id`` ...) instead of a model argument. Strings are
returned as-is; other results are sent to the model as JSON.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import re
import typing
from typing import Any, Callable, Dict, Optional, Tuple, Type

from pydantic import Field, create_model

from xuloom.tools.base import BaseAgenticTool

__all__ = ["tool", "function_tool"]

_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")
_ARGS_HEADER = re.compile(r"^\s*(Args|Arguments|Parameters)\s*:\s*$")
_ARG_LINE = re.compile(r"^\s+(\*{0,2}\w+)\s*(?:\([^)]*\))?\s*:\s*(.*)$")


def _parse_docstring(doc: str) -> Tuple[str, Dict[str, str]]:
    """First paragraph, and ``{name: description}`` from a Google-style Args section."""
    lines = doc.splitlines()
    summary: list = []
    for line in lines:
        if not line.strip() or _ARGS_HEADER.match(line):
            break
        summary.append(line.strip())
    described: Dict[str, str] = {}
    start = next((index + 1 for index, line in enumerate(lines) if _ARGS_HEADER.match(line)), None)
    if start is None:
        return " ".join(summary).strip(), described
    current: Optional[str] = None
    entry_indent: Optional[int] = None
    for line in lines[start:]:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            break  # next section (Returns:, Raises: ...)
        if entry_indent is None:
            entry_indent = indent
        match = _ARG_LINE.match(line)
        if indent == entry_indent and match:
            current = match.group(1).lstrip("*")
            described[current] = match.group(2).strip()
        elif current is not None:  # continuation line of the current argument
            described[current] = (described[current] + " " + line.strip()).strip()
    return " ".join(summary).strip(), described


def function_tool(fn: Callable, *, name: Optional[str] = None, description: Optional[str] = None,
                  read_only: bool = False, risk_level: Optional[str] = None,
                  workspace_paths: Optional[Dict[str, bool]] = None) -> Type[BaseAgenticTool]:
    """Build a :class:`BaseAgenticTool` subclass from ``fn`` (see the module docstring)."""
    if not callable(fn):
        raise TypeError("tool() expects a function")
    tool_name: str = str(name or getattr(fn, "__name__", "") or "")
    if not _NAME.fullmatch(tool_name):
        raise ValueError(f"Invalid tool name {tool_name!r}: use letters, digits, '_' or '-' (max 64), starting with a letter")
    summary, argument_docs = _parse_docstring(inspect.getdoc(fn) or "")
    tool_description = (description or summary or "").strip()
    if not tool_description:
        raise ValueError(f"Tool {tool_name!r} needs a description: add a docstring or pass description=")
    if risk_level is not None and risk_level not in {"low", "medium", "high"}:
        raise ValueError("risk_level must be 'low', 'medium' or 'high'")
    signature = inspect.signature(fn)
    hints = typing.get_type_hints(fn, include_extras=True)
    fields: Dict[str, Any] = {}
    wants_context = False
    for parameter in signature.parameters.values():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            raise TypeError(f"Tool {tool_name!r}: *args/**kwargs cannot be described to a model")
        if parameter.kind is parameter.POSITIONAL_ONLY:
            raise TypeError(f"Tool {tool_name!r}: positional-only parameters are not supported")
        if parameter.name == "context":
            wants_context = True
            continue
        annotation = hints.get(parameter.name, Any)
        default = ... if parameter.default is parameter.empty else parameter.default
        fields[parameter.name] = (annotation, Field(default, description=argument_docs.get(parameter.name)))
    class_name = "".join(part.capitalize() for part in re.split(r"[_-]+", tool_name)) or "Function"
    input_model = create_model(f"{class_name}Input", **fields)
    is_async = inspect.iscoroutinefunction(fn)

    async def execute(self, **kwargs):
        if wants_context:
            kwargs["context"] = self.context
        result = await fn(**kwargs) if is_async else await asyncio.to_thread(fn, **kwargs)
        if isinstance(result, str):
            return result
        return "" if result is None else json.dumps(result, ensure_ascii=False, default=str)

    attributes = {
        "name": tool_name,
        "description": tool_description,
        "input_schema": input_model,
        "is_read_only": bool(read_only),
        "risk_level": risk_level or ("low" if read_only else "high"),
        "workspace_paths": dict(workspace_paths or {}),
        "execute": execute,
        "function": staticmethod(fn),
        "__doc__": tool_description,
        "__module__": getattr(fn, "__module__", __name__),
        "__qualname__": f"{class_name}Tool",
    }
    return type(f"{class_name}Tool", (BaseAgenticTool,), attributes)


def tool(fn: Optional[Callable] = None, *, name: Optional[str] = None, description: Optional[str] = None,
         read_only: bool = False, risk_level: Optional[str] = None, workspace_paths: Optional[Dict[str, bool]] = None):
    """Decorator form of :func:`function_tool`: ``@tool`` or ``@tool(read_only=True, name=...)``."""
    def build(target: Callable) -> Type[BaseAgenticTool]:
        return function_tool(target, name=name, description=description, read_only=read_only,
                             risk_level=risk_level, workspace_paths=workspace_paths)
    return build(fn) if fn is not None else build


def as_tool_instance(candidate) -> BaseAgenticTool:
    """Accept a tool instance or a tool class (instantiated per engine)."""
    if isinstance(candidate, type) and issubclass(candidate, BaseAgenticTool):
        return candidate()
    if isinstance(candidate, BaseAgenticTool):
        return candidate
    raise TypeError("Custom tools must be BaseAgenticTool instances or subclasses (for example from @tool)")
