"""Names of ``xuloom <command>`` entry points; kept light for interactive startup."""
import re
import sys

_PLUGIN_COMMANDS = None
_FAILED_COMMANDS: dict = {}


def plugin_commands():
    """``xuloom <command>`` entries contributed by bundled and enabled plugins."""
    global _PLUGIN_COMMANDS
    if _PLUGIN_COMMANDS is None:
        from xuloom.plugins import default_plugin_names, load_plugins
        commands = {}
        try:
            for plugin in load_plugins(default_plugin_names(), strict=False):
                if plugin.ok:
                    for name, entry in plugin.api.cli_commands.items():
                        commands.setdefault(name, entry)
                    plugin.rollback_adapters()  # engines opened by a command register their own adapters
        except Exception as error:
            print(f"Plugin configuration could not be read: {error}", file=sys.stderr)
        _PLUGIN_COMMANDS = commands
    return _PLUGIN_COMMANDS


BUILTIN_COMMANDS = frozenset({"exec", "goal", "schedule", "pipeline", "plugins", "git-status", "chat-link", "summary",
                              "transcript", "remote", "sandbox-cleanup", "credentials", "mcp", "search-config",
                              "context", "scaling", "eval", "serve"})


def mark_command_failed(name: str, error: BaseException) -> None:
    """Drop a plugin command whose argument setup failed; warn once."""
    if name not in _FAILED_COMMANDS:
        _FAILED_COMMANDS[name] = f"{type(error).__name__}: {error}"
        print(f"Plugin command '{name}' is unavailable: {_FAILED_COMMANDS[name]}", file=sys.stderr)


def command_names():
    return BUILTIN_COMMANDS | set(plugin_commands())


def is_command(argument: str) -> bool:
    """True when ``argument`` names a non-interactive command (built-in or from a plugin)."""
    if argument in BUILTIN_COMMANDS:
        return True
    return bool(re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", argument or "")) and argument in plugin_commands()


def disabled_command_hint(argument: str):
    """Explain why a bundled plugin's command is missing instead of treating it as a chat prompt."""
    from xuloom.plugins import BUNDLED_PLUGINS, disabled_plugin_names
    try:
        if argument in BUNDLED_PLUGINS and argument in disabled_plugin_names():
            return f"The bundled '{argument}' plugin is disabled. Run: xuloom plugins enable {argument}"
    except Exception:
        return None
    return None
