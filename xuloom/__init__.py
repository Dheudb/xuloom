"""Xuloom — a general-purpose agent framework and desktop workbench.

Stable public API (everything else, including ``_``-prefixed members and the
``engine.available_tools`` / ``engine.tool_context`` dictionaries, is internal)::

    from xuloom import QueryEngine, EngineConfig, BaseAgenticTool, RunResult

Names are imported lazily so ``import xuloom`` stays cheap for CLI startup.
"""
from importlib import import_module
from typing import TYPE_CHECKING

__version__ = "0.1.48"

_EXPORTS = {
    "QueryEngine": "xuloom.core.engine",
    "EngineConfig": "xuloom.core.engine_config",
    "AbortRequestedError": "xuloom.core.model_stream",
    "RunResult": "xuloom.core.run_result",
    "PersistenceError": "xuloom.core.run_result",
    "BaseAgenticTool": "xuloom.tools.base",
    "tool": "xuloom.tools.function",
    "ApprovalPolicy": "xuloom.security.permissions",
    "default_tools": "xuloom.tools.builtin",
    "ModelAdapter": "xuloom.models",
    "ToolCall": "xuloom.models",
    "model_response": "xuloom.models",
    "register_adapter": "xuloom.models",
    "PluginAPI": "xuloom.plugins",
    "HOOK_EVENTS": "xuloom.core.hooks",
    "Span": "xuloom.observability",
    "Tracer": "xuloom.observability",
    "TraceExporter": "xuloom.observability",
    "JsonlTraceExporter": "xuloom.observability",
    "ModelPrice": "xuloom.observability",
}

__all__ = ["__version__", *_EXPORTS]


def __getattr__(name):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module 'xuloom' has no attribute {name!r}")
    value = getattr(import_module(module), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_EXPORTS))


if TYPE_CHECKING:  # pragma: no cover - static analysers see real imports
    from xuloom.core.engine import QueryEngine
    from xuloom.core.engine_config import EngineConfig
    from xuloom.core.model_stream import AbortRequestedError
    from xuloom.core.run_result import PersistenceError, RunResult
    from xuloom.security.permissions import ApprovalPolicy
    from xuloom.tools.base import BaseAgenticTool
    from xuloom.tools.function import tool
    from xuloom.tools.builtin import default_tools
    from xuloom.core.hooks import HOOK_EVENTS
    from xuloom.models import ModelAdapter, ToolCall, model_response, register_adapter
    from xuloom.plugins import PluginAPI
    from xuloom.observability import JsonlTraceExporter, ModelPrice, Span, TraceExporter, Tracer
