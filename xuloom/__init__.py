"""Xuloom — a general-purpose agent framework and desktop workbench.

Stable public API (everything else, including ``_``-prefixed members and the
``engine.available_tools`` / ``engine.tool_context`` dictionaries, is internal)::

    from xuloom import QueryEngine, EngineConfig, BaseAgenticTool, RunResult

Names are imported lazily so ``import xuloom`` stays cheap for CLI startup.
"""
from importlib import import_module
from typing import TYPE_CHECKING

__version__ = "0.1.40"

_EXPORTS = {
    "QueryEngine": "xuloom.core.engine",
    "EngineConfig": "xuloom.core.engine_config",
    "AbortRequestedError": "xuloom.core.model_stream",
    "RunResult": "xuloom.core.run_result",
    "PersistenceError": "xuloom.core.run_result",
    "BaseAgenticTool": "xuloom.tools.base",
    "ApprovalPolicy": "xuloom.security.permissions",
    "default_tools": "xuloom.tools.builtin",
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
    from xuloom.tools.builtin import default_tools
