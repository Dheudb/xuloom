"""Load desktop automation only after local opt-in."""
import os
import sys

NAME = "computer-use"


def supported():
    return sys.platform == "win32" and os.environ.get("XULOOM_REMOTE") != "1"


def enabled():
    from xuloom.plugins import enabled_plugin_names
    return supported() and NAME in enabled_plugin_names()


def settings(method, params):
    if method == "computerUse.set":
        if not supported() or type(params.get("enabled")) is not bool:
            raise ValueError("Computer Use can only be configured on local Windows.")
        from xuloom.plugins import set_plugin_enabled
        set_plugin_enabled(NAME, params["enabled"])
    return {"supported": supported(), "enabled": enabled()}


def register(api):
    if not supported():
        return
    from .tool import ComputerUseTool
    tool = ComputerUseTool()
    api.add_tool(tool, latent=True)
    for event in ("UserPromptSubmit", "TurnEnd", "TurnError", "SessionEnd"):
        api.add_hook(event, tool.reset)
