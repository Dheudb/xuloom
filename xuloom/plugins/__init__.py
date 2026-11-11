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
from typing import Any, Callable, Dict, Iterable, List, Optional

__all__ = ["PluginAPI", "RpcContext", "LoadedPlugin", "PluginError", "load_plugins", "available_plugins",
           "enabled_plugin_names", "disabled_plugin_names", "set_plugin_enabled", "default_plugin_names",
           "plugins_home", "BUNDLED_PLUGINS", "ENTRY_POINT_GROUP"]

ENTRY_POINT_GROUP = "xuloom.plugins"
#: Plugins shipped with Xuloom. They are on by default; users may disable them.
BUNDLED_PLUGINS = {"media": "xuloom.media.plugin"}
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
        self.adapters: List[Any] = []
        self.cli_commands: Dict[str, tuple] = {}
        self.rpc_methods: Dict[str, Callable] = {}
        self.rpc_options: Dict[str, dict] = {}
        self.trace_exporters: List[Any] = []

    def add_tool(self, tool, *, latent: bool = False) -> None:
        """Add a trusted tool: a ``BaseAgenticTool`` subclass (e.g. from ``@tool``) or a new instance.

        ``latent=True`` installs the tool hidden: the model finds and activates it
        through ``tool_search_tool``, which keeps rarely used tools out of every request.
        """
        from xuloom.tools.function import as_tool_instance
        self.tools.append((as_tool_instance(tool), bool(latent)))

    def add_cli_command(self, name: str, configure: Callable, handler: Callable, *, help: str = "") -> None:
        """Add ``xuloom <name> ...`` to the non-interactive CLI.

        ``configure(parser)`` adds arguments to an ``argparse`` sub-parser;
        ``async handler(args, emit)`` returns a JSON-serialisable dict (``text``
        is printed without ``--json``; ``ok``/``reason`` set the exit status).
        """
        if not _NAME.fullmatch(name or ""):
            raise PluginError("CLI command names use lowercase letters, digits, '-' or '_'")
        if not callable(configure) or not callable(handler):
            raise TypeError("configure and handler must be callable")
        if name in self.cli_commands:
            raise PluginError(f"CLI command {name!r} is added twice")
        self.cli_commands[name] = (configure, handler, help)

    def add_rpc_method(self, name: str, handler: Callable, *, operation: bool = False, settings: bool = False) -> None:
        """Serve a desktop RPC method ``"<prefix>.<action>"`` with ``async handler(params, context)``.

        ``context`` is a :class:`RpcContext`. Built-in methods always take precedence.
        ``operation=True`` marks methods that start a long operation in the
        conversation (the app then treats the conversation as busy);
        ``settings=True`` marks methods that change app-wide settings (the app
        refuses them while any conversation is running).
        """
        if not re.fullmatch(r"[a-z][a-z0-9_-]*\.[A-Za-z][A-Za-z0-9_]*", name or ""):
            raise PluginError("RPC methods are named '<prefix>.<action>'")
        if not callable(handler):
            raise TypeError("RPC handler must be callable")
        if name in self.rpc_methods:
            raise PluginError(f"RPC method {name!r} is added twice")
        self.rpc_methods[name] = handler
        self.rpc_options[name] = {"operation": bool(operation), "settings": bool(settings)}

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

    def add_trace_exporter(self, exporter) -> None:
        """Receive every finished run/model/tool span (``exporter.export(span)``; see :mod:`xuloom.observability`).

        Subagents share their parent's tracer, so an exporter sees their spans once, nested
        under the tool call that started them.
        """
        if not callable(getattr(exporter, "export", None)):
            raise TypeError("Trace exporters need an export(span) method")
        self.trace_exporters.append(exporter)

    def add_skills(self, directory) -> None:
        """Expose ``<directory>/<skill>/SKILL.md`` skills."""
        self.skill_dirs.append(_existing_dir(directory))

    def add_agents(self, directory) -> None:
        """Expose ``<directory>/<role>.md`` subagent definitions."""
        self.agent_dirs.append(_existing_dir(directory))


class RpcContext:
    """What a desktop RPC handler of a plugin may use (provided by the desktop bridge)."""

    #: Workspace directory of the window.
    cwd: str = ""
    #: Current conversation ID, or ``None`` before one is opened.
    session_id: Optional[str] = None

    def require_idle(self) -> None:
        """Raise ``ValueError`` while a task, worker or upload is running."""
        raise NotImplementedError

    async def engine(self, *, write: bool = False):
        """The window's QueryEngine (created on demand; ``write=True`` takes the session writer)."""
        raise NotImplementedError

    def emit(self, event: dict) -> None:
        """Send an event to the desktop UI."""
        raise NotImplementedError

    def start_operation(self, name: str, work: Callable[[], Any]) -> None:
        """Run ``await work()`` as the window's current operation (stoppable from the UI)."""
        raise NotImplementedError


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


def _read_plugin_settings(home: Optional[Path]) -> dict:
    path = plugins_home(home) / "plugins.json"
    if not path.exists():
        return {"enabled": [], "disabled": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise PluginError(f"{path} must be a JSON object")
    result = {}
    for key in ("enabled", "disabled"):
        names = data.get(key, [])
        if not isinstance(names, list) or not all(isinstance(item, str) and _NAME.fullmatch(item) for item in names):
            raise PluginError(f"{path}: \"{key}\" must be a list of plugin names")
        result[key] = list(dict.fromkeys(names))
    return result


def enabled_plugin_names(home: Optional[Path] = None) -> List[str]:
    """Installed (non-bundled) plugins the user enabled."""
    return [name for name in _read_plugin_settings(home)["enabled"] if name not in BUNDLED_PLUGINS]


def disabled_plugin_names(home: Optional[Path] = None) -> List[str]:
    """Bundled plugins the user turned off."""
    return [name for name in _read_plugin_settings(home)["disabled"] if name in BUNDLED_PLUGINS]


def default_plugin_names(home: Optional[Path] = None, *, bundled: bool = True, user: bool = True,
                         exclude: Iterable[str] = ()) -> List[str]:
    """Names an app session loads: bundled plugins (unless disabled) then the user's enabled plugins."""
    skip = set(exclude)
    names = []
    if bundled:
        disabled = set(disabled_plugin_names(home)) if user else set()
        names += [name for name in BUNDLED_PLUGINS if name not in disabled]
    if user:
        names += enabled_plugin_names(home)
    return [name for name in dict.fromkeys(names) if name not in skip]


def set_plugin_enabled(name: str, enabled: bool, home: Optional[Path] = None) -> List[str]:
    """Enable/disable a plugin for new sessions; returns the plugins that will load by default."""
    if not _NAME.fullmatch(name or ""):
        raise PluginError("Plugin names use lowercase letters, digits, '-' or '_'")
    settings = _read_plugin_settings(home)
    if name in BUNDLED_PLUGINS:
        settings["disabled"] = [item for item in settings["disabled"] if item != name] + ([] if enabled else [name])
    else:
        settings["enabled"] = [item for item in settings["enabled"] if item != name] + ([name] if enabled else [])
    path = plugins_home(home) / "plugins.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    from xuloom.core.storage import atomic_json
    atomic_json(path, {"enabled": settings["enabled"], "disabled": settings["disabled"]})
    return default_plugin_names(home)


def _entry_points():
    from importlib.metadata import entry_points
    found = entry_points()
    if hasattr(found, "select"):
        return list(found.select(group=ENTRY_POINT_GROUP))
    return list(found.get(ENTRY_POINT_GROUP, []))  # Python 3.9


def available_plugins(home: Optional[Path] = None) -> List[dict]:
    """Bundled and installed plugins (entry points, ``~/.xuloom/plugins/<name>``) with their state."""
    enabled = set(default_plugin_names(home))
    rows = {name: {"name": name, "source": "bundled:" + module, "bundled": True} for name, module in BUNDLED_PLUGINS.items()}
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
    if name in BUNDLED_PLUGINS:
        return importlib.import_module(BUNDLED_PLUGINS[name]), "bundled:" + BUNDLED_PLUGINS[name]
    for point in _entry_points():
        if point.name == name:
            return point.load(), "package:" + point.value
    path = plugins_home(home) / "plugins" / name / "plugin.py"
    if path.is_file():
        module_name = "xuloom_plugin_" + name.replace("-", "_")
        module = sys.modules.get(module_name)
        if module is None or getattr(module, "__file__", None) != str(path):
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise PluginError(f"Cannot load plugin file {path}")
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
