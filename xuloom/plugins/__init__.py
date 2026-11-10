"""Plugins: package tools, lifecycle hooks, model adapters, Skills and Subagents together.

A plugin is any of:

* an object with ``name`` and ``register(api)``;
* a module (or ``plugin.py`` file) defining ``register(api)`` and optionally ``NAME``;
* a function ``register(api)``.

``register`` receives a :class:`PluginAPI` and is called once for every engine
that loads the plugin, so it should only create objects and call ``api.add_*``.

Loading is always explicit. Hosts pass plugins with
``EngineConfig(plugins=[...])``. The CLI and desktop app load the plugins
named in ``~/.xuloom/plugins.json`` (``{"enabled": ["name", ...]}``), resolved
from installed packages (entry point group ``xuloom.plugins``) or from
``~/.xuloom/plugins/<name>/plugin.py``. Plugin code runs with the same
rights as Xuloom itself; a workspace cannot enable plugins for you.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterable, List, Optional

__all__ = ["PluginAPI", "LoadedPlugin", "PluginError", "load_plugins", "available_plugins",
           "enabled_plugin_names", "set_plugin_enabled", "plugins_home", "ENTRY_POINT_GROUP"]

ENTRY_POINT_GROUP = "xuloom.plugins"
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_logger = logging.getLogger("xuloom.plugins")


class PluginError(RuntimeError):
    pass


class PluginAPI:
    """What a plugin may contribute to one engine."""

    def __init__(self, name: str):
        self.name = name
        self.tools: List[Any] = []
        self.hooks: List[tuple] = []
        self.skill_dirs: List[Path] = []
        self.agent_dirs: List[Path] = []
        self.adapters: List[str] = []

    def add_tool(self, tool) -> None:
        """Add a trusted ``BaseAgenticTool`` instance (create a new one per call to ``register``)."""
        from xuloom.tools.base import BaseAgenticTool
        if not isinstance(tool, BaseAgenticTool):
            raise TypeError("Plugin tools must be BaseAgenticTool instances")
        self.tools.append(tool)

    def add_hook(self, event: str, callback: Callable, *, tools: Optional[Iterable[str]] = None,
                 modes: Optional[Iterable[str]] = None) -> None:
        """Add a lifecycle hook (see ``xuloom.core.hooks.HOOK_EVENTS``)."""
        from xuloom.core.hooks import HOOK_EVENT_ALIASES, HOOK_EVENTS
        if HOOK_EVENT_ALIASES.get(event, event) not in HOOK_EVENTS:
            raise ValueError("Unknown hook event: " + str(event))
        if not callable(callback):
            raise TypeError("Hook callback must be callable")
        self.hooks.append((event, callback, tools, modes))

    def add_model_adapter(self, adapter) -> None:
        """Register a :class:`xuloom.models.ModelAdapter` (process-wide, owned by this plugin).

        Registration happens only after ``register(api)`` returns successfully.
        """
        from xuloom.models.adapter import ModelAdapter
        if not isinstance(adapter, ModelAdapter):
            raise TypeError("Model adapters must subclass xuloom.models.ModelAdapter")
        self.adapters.append(adapter)

    def _commit_adapters(self) -> dict:
        """Register collected adapters all-or-nothing; returns the rollback snapshot."""
        from xuloom.models.registry import _restore, _snapshot, register_adapter
        snapshot = _snapshot([str(adapter.name or "") for adapter in self.adapters])
        try:
            for adapter in self.adapters:
                register_adapter(adapter, owner="plugin:" + self.name)
        except BaseException:
            _restore(snapshot)
            raise
        return snapshot

    def add_skills(self, directory) -> None:
        """Expose ``<directory>/<skill>/SKILL.md`` skills."""
        self.skill_dirs.append(_existing_dir(directory))

    def add_agents(self, directory) -> None:
        """Expose ``<directory>/<role>.md`` subagent definitions."""
        self.agent_dirs.append(_existing_dir(directory))


@dataclass
class LoadedPlugin:
    name: str
    source: str
    spec: Any
    api: Optional[PluginAPI] = None
    error: str = ""
    adapter_snapshot: Optional[dict] = None

    def rollback_adapters(self) -> None:
        """Undo this plugin's adapter registrations (restoring whatever was there before)."""
        if self.adapter_snapshot is not None:
            from xuloom.models.registry import _restore
            _restore(self.adapter_snapshot)
            self.adapter_snapshot = None

    @property
    def ok(self) -> bool:
        return not self.error


def _existing_dir(directory) -> Path:
    path = Path(directory).expanduser().resolve()
    if not path.is_dir():
        raise PluginError(f"Plugin directory does not exist: {path}")
    return path


def plugins_home(home: Optional[Path] = None) -> Path:
    return Path(home or Path.home()) / ".xuloom"


def enabled_plugin_names(home: Optional[Path] = None) -> List[str]:
    path = plugins_home(home) / "plugins.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    names = data.get("enabled", []) if isinstance(data, dict) else None
    if not isinstance(names, list) or not all(isinstance(item, str) and _NAME.fullmatch(item) for item in names):
        raise PluginError(f"{path} must contain {{\"enabled\": [plugin names]}}")
    return list(dict.fromkeys(names))


def set_plugin_enabled(name: str, enabled: bool, home: Optional[Path] = None) -> List[str]:
    if not _NAME.fullmatch(name or ""):
        raise PluginError("Plugin names use lowercase letters, digits, '-' or '_'")
    names = [item for item in enabled_plugin_names(home) if item != name] + ([name] if enabled else [])
    path = plugins_home(home) / "plugins.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    from xuloom.core.storage import atomic_json
    atomic_json(path, {"enabled": names})
    return names


def _entry_points():
    from importlib.metadata import entry_points
    found = entry_points()
    if hasattr(found, "select"):
        return list(found.select(group=ENTRY_POINT_GROUP))
    return list(found.get(ENTRY_POINT_GROUP, []))  # Python 3.9


def available_plugins(home: Optional[Path] = None) -> List[dict]:
    """Installed plugins (entry points and ``~/.xuloom/plugins/<name>``) with their enabled state."""
    enabled = set(enabled_plugin_names(home))
    rows = {}
    for point in _entry_points():
        rows.setdefault(point.name, {"name": point.name, "source": "package:" + point.value})
    root = plugins_home(home) / "plugins"
    if root.is_dir():
        for path in sorted(root.iterdir()):
            if (path / "plugin.py").is_file() and _NAME.fullmatch(path.name):
                rows.setdefault(path.name, {"name": path.name, "source": str(path / "plugin.py")})
    for name in enabled - set(rows):
        rows[name] = {"name": name, "source": "", "missing": True}
    return [{**row, "enabled": row["name"] in enabled} for _, row in sorted(rows.items())]


def _resolve_name(name: str, home: Optional[Path]):
    for point in _entry_points():
        if point.name == name:
            return point.load(), "package:" + point.value
    path = plugins_home(home) / "plugins" / name / "plugin.py"
    if path.is_file():
        module_name = "xuloom_plugin_" + name.replace("-", "_")
        module = sys.modules.get(module_name)
        if module is None or getattr(module, "__file__", None) != str(path):
            spec = importlib.util.spec_from_file_location(module_name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                sys.modules.pop(module_name, None)
                raise
        return module, str(path)
    raise PluginError(f"Plugin {name!r} is enabled but not installed")


def _identify(target, fallback: str):
    if isinstance(target, str):
        return target
    name = getattr(target, "NAME", None) or getattr(target, "name", None)
    if isinstance(target, ModuleType) and not name:
        name = target.__name__.rsplit(".", 1)[-1]
    if callable(target) and not name:
        name = getattr(target, "__name__", None)
    return str(name or fallback)


def _register(target, name: str) -> PluginAPI:
    register = target if (callable(target) and not hasattr(target, "register")) else getattr(target, "register", None)
    if not callable(register):
        raise PluginError(f"Plugin {name!r} has no register(api) function")
    api = PluginAPI(name)
    register(api)
    return api


def load_plugins(specs: Iterable[Any], *, home: Optional[Path] = None, strict: bool = True) -> List[LoadedPlugin]:
    """Load plugins from objects, modules, callables or enabled names.

    ``strict`` raises the first failure; otherwise failures are returned with
    ``error`` set and logged, so one broken plugin does not stop the app.
    A plugin whose ``register`` fails contributes nothing.
    """
    loaded, seen = [], set()
    for index, spec in enumerate(specs):
        name = _identify(spec, f"plugin-{index + 1}")
        if name in seen:
            continue
        seen.add(name)
        source = "object"
        try:
            target = spec
            if isinstance(spec, str):
                if not _NAME.fullmatch(spec):
                    raise PluginError(f"Invalid plugin name: {spec!r}")
                target, source = _resolve_name(spec, home)
            api = _register(target, name)
            snapshot = api._commit_adapters()
            loaded.append(LoadedPlugin(name, source, spec, api, adapter_snapshot=snapshot))
        except Exception as error:
            if strict:
                # Nothing from a failed explicit load stays registered.
                for plugin in reversed(loaded):
                    plugin.rollback_adapters()
                raise PluginError(f"Plugin {name!r} failed to load: {error}") from error
            _logger.warning("Plugin %s failed to load: %s", name, error)
            loaded.append(LoadedPlugin(name, source, spec, None, f"{type(error).__name__}: {error}"))
    return loaded
