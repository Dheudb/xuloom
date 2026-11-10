"""Process-wide registry of model adapters."""
from __future__ import annotations

import re
import threading
import weakref
from typing import Dict, List, Optional

from xuloom.models.adapter import ModelAdapter

__all__ = ["register_adapter", "unregister_adapter", "get_adapter", "adapter_names", "is_native_provider",
           "bind_client", "unbind_client", "client_adapter"]

_LOCK = threading.Lock()
_ADAPTERS: Dict[str, ModelAdapter] = {}
_OWNERS: Dict[str, Optional[str]] = {}
_NAME = re.compile(r"[a-z][a-z0-9_.-]{0,63}")


def register_adapter(adapter: ModelAdapter, *, replace: bool = False, owner: Optional[str] = None) -> str:
    """Register ``adapter`` under ``adapter.name``. Built-in adapters can never be replaced.

    ``owner`` lets the same plugin re-register its adapter (once per engine)
    without ``replace=True``; another owner still needs ``replace=True``.
    """
    if not isinstance(adapter, ModelAdapter):
        raise TypeError("Model adapters must subclass xuloom.models.ModelAdapter")
    name = str(adapter.name or "")
    if not _NAME.fullmatch(name):
        raise ValueError("Adapter name must be lowercase letters, digits, '.', '_' or '-' (max 64)")
    with _LOCK:
        _ensure_builtins()
        current = _ADAPTERS.get(name)
        same_owner = owner is not None and _OWNERS.get(name) == owner
        if current is not None and (current.native or not (replace or same_owner)):
            if current is adapter:
                return name
            raise ValueError("Model adapter already registered: " + name)
        _ADAPTERS[name] = adapter
        _OWNERS[name] = owner
    return name


def _snapshot(names) -> Dict[str, tuple]:
    """Current (adapter, owner) per name, or None when absent; used to roll back plugin loads."""
    with _LOCK:
        _ensure_builtins()
        return {name: ((_ADAPTERS[name], _OWNERS.get(name)) if name in _ADAPTERS else None) for name in names}


def _restore(snapshot: Dict[str, tuple]) -> None:
    with _LOCK:
        for name, previous in snapshot.items():
            if previous is None:
                _ADAPTERS.pop(name, None)
                _OWNERS.pop(name, None)
            else:
                _ADAPTERS[name], _OWNERS[name] = previous


def unregister_adapter(name: str) -> None:
    with _LOCK:
        adapter = _ADAPTERS.get(name)
        if adapter is not None and adapter.native:
            raise ValueError("Built-in model adapters cannot be removed")
        _ADAPTERS.pop(name, None)
        _OWNERS.pop(name, None)


def get_adapter(name: Optional[str]) -> Optional[ModelAdapter]:
    with _LOCK:
        _ensure_builtins()
        return _ADAPTERS.get(str(name or "").strip().lower())


def adapter_names() -> List[str]:
    with _LOCK:
        _ensure_builtins()
        return sorted(_ADAPTERS)


def is_native_provider(name: Optional[str]) -> bool:
    adapter = get_adapter(name)
    return adapter is None or adapter.native


def _ensure_builtins():
    if "openai" not in _ADAPTERS:
        from xuloom.models.builtin import AnthropicAdapter, OpenAIAdapter
        _ADAPTERS.setdefault("openai", OpenAIAdapter())
        _ADAPTERS.setdefault("anthropic", AnthropicAdapter())


# Each client remembers the adapter instance that created it, so a later
# re-registration (another engine loading the same plugin) never pairs this
# client with a different adapter object.
_CLIENT_ADAPTERS = weakref.WeakKeyDictionary()
_CLIENT_ADAPTERS_BY_ID: Dict[int, tuple] = {}


def bind_client(client, adapter: ModelAdapter):
    if client is None:
        return client
    _prune_closed()
    try:
        _CLIENT_ADAPTERS[client] = adapter
    except TypeError:  # not weak-referenceable: keep a strong pair keyed by id until closed
        _CLIENT_ADAPTERS_BY_ID[id(client)] = (client, adapter)
    return client


def unbind_client(client) -> None:
    """Forget a client (called when an engine closes or discards it)."""
    if client is None:
        return
    try:
        _CLIENT_ADAPTERS.pop(client, None)
    except TypeError:
        pass
    pair = _CLIENT_ADAPTERS_BY_ID.get(id(client))
    if pair is not None and pair[0] is client:
        del _CLIENT_ADAPTERS_BY_ID[id(client)]


def _prune_closed() -> None:
    # Strong references are only for clients that cannot be weakly referenced;
    # drop any that were closed without an explicit unbind.
    for key, (client, _) in list(_CLIENT_ADAPTERS_BY_ID.items()):
        try:
            closed = bool(getattr(client, "is_closed", False))
        except Exception:
            closed = False
        if closed:
            _CLIENT_ADAPTERS_BY_ID.pop(key, None)


def client_adapter(client) -> Optional[ModelAdapter]:
    if client is None:
        return None
    try:
        found = _CLIENT_ADAPTERS.get(client)
    except TypeError:
        found = None
    if found is None:
        pair = _CLIENT_ADAPTERS_BY_ID.get(id(client))
        found = pair[1] if pair and pair[0] is client else None
    return found
