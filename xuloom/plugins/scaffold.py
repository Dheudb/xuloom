"""``xuloom plugins new <name>``: a runnable plugin template with its own offline test."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

__all__ = ["create_plugin"]

PLUGIN_TEMPLATE = '''"""{name}: a Xuloom plugin.

Enable it with `xuloom plugins enable {name}` (CLI and desktop app), or pass the module to
EngineConfig(plugins=[...]). Run `python test_plugin.py` to check it without a model.
See https://github.com/Dheudb/xuloom/blob/main/docs/plugins.md for everything a plugin can add.
"""
from xuloom import tool

NAME = "{name}"


@tool(read_only=True)
def {ident}_hello(person: str) -> str:
    """Greet someone. (Example tool: replace it with your own.)

    Args:
        person: Who to greet.
    """
    return f"Hello, {{person}}!"


def note_tool_use(payload):
    """Example hook: runs after every tool call.

    Return {{"context": "..."}} to add a note for the model, or None to stay silent.
    """
    return None


def register(api):
    """Called once per engine: add tools, hooks, model adapters, skills, CLI commands ..."""
    api.add_tool({ident}_hello)
    api.add_hook("AfterToolUse", note_tool_use)
'''

TEST_TEMPLATE = '''"""Offline check for the {name} plugin: python test_plugin.py (no API key or network needed)."""
import asyncio
import importlib.util
import os
import pathlib
import tempfile

HERE = pathlib.Path(__file__).resolve().parent


async def main():
    with tempfile.TemporaryDirectory() as folder:
        os.environ["XULOOM_DATA_DIR"] = os.path.join(folder, "data")  # keep test sessions out of your history
        from xuloom import ApprovalPolicy, EngineConfig, QueryEngine
        from xuloom.models import ToolCall, model_response
        from xuloom.testing import ScriptedModel

        spec = importlib.util.spec_from_file_location("{ident}_plugin", HERE / "plugin.py")
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)

        # A scripted model calls the example tool once, then answers.
        model = ScriptedModel([
            model_response(tool_calls=[ToolCall("call-1", "{ident}_hello", {{"person": "Ada"}})]),
            model_response(text="done"),
        ])
        with model:
            config = model.config(EngineConfig(cwd=folder, plugins=[plugin], approval_policy=ApprovalPolicy("full")))
            async with QueryEngine(config) as engine:
                result = await engine.run("say hello", sys_print_callback=lambda *_: None)
    assert result.ok, result
    tool_results = [block for message in model.requests[1].messages for block in message.get("content", [])
                    if isinstance(block, dict) and block.get("type") == "tool_result"]
    assert "Hello, Ada!" in str(tool_results[0]["content"]), tool_results
    print("{name}: plugin OK")


if __name__ == "__main__":
    asyncio.run(main())
'''

README_TEMPLATE = '''# {name}

A Xuloom plugin created with `xuloom plugins new {name}`.

- `plugin.py`: `register(api)` adds an example tool (`{ident}_hello`, written with `@tool`) and an `AfterToolUse` hook.
- `test_plugin.py`: runs the plugin against a scripted model, offline: `python test_plugin.py`.

Enable it for the CLI and desktop app with `xuloom plugins enable {name}` (it applies to new sessions),
check it with `xuloom plugins list`, and disable it again with `xuloom plugins disable {name}`.

Docs: [plugins](https://github.com/Dheudb/xuloom/blob/main/docs/plugins.md) ·
[hooks](https://github.com/Dheudb/xuloom/blob/main/docs/hooks.md) ·
[model adapters](https://github.com/Dheudb/xuloom/blob/main/docs/model-adapters.md)
'''


def create_plugin(name: str, *, home: Optional[Path] = None, directory: Optional[Path] = None) -> Path:
    """Write the template to ``<directory or ~/.xuloom/plugins>/<name>/`` and return that folder."""
    from xuloom.plugins import _NAME, BUNDLED_PLUGINS, PluginError, plugins_home
    if not _NAME.fullmatch(name or ""):
        raise PluginError("Plugin names use lowercase letters, digits, '-' or '_' and start with a letter")
    if name in BUNDLED_PLUGINS:
        raise PluginError(f"{name!r} is the name of a bundled plugin")
    parent = Path(directory) if directory is not None else plugins_home(home) / "plugins"
    target = parent / name
    if target.exists():
        raise PluginError(f"{target} already exists; choose another name or remove it first")
    ident = name.replace("-", "_")
    target.mkdir(parents=True)
    values = {"name": name, "ident": ident}
    (target / "plugin.py").write_text(PLUGIN_TEMPLATE.format(**values), encoding="utf-8")
    (target / "test_plugin.py").write_text(TEST_TEMPLATE.format(**values), encoding="utf-8")
    (target / "README.md").write_text(README_TEMPLATE.format(**values), encoding="utf-8")
    return target
