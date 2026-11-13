"""Bind desktop input to an approved window and one current screenshot."""
import asyncio
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from xuloom.context.tool_results import build_tool_result
from xuloom.core.model_inputs import UnsupportedInput, validate_media
from xuloom.security.execution import SandboxUnavailable
from xuloom.tools.base import BaseAgenticTool

from .plugin import enabled


class ComputerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["list", "observe", "move", "click", "type", "scroll", "key"]
    window: str | None = Field(None, description="Window ID from list, required for observe.")
    snapshot: str | None = Field(None, description="Latest snapshot ID from observe, required for input.")
    x: int | None = Field(None, ge=0, strict=True, description="X pixel in the returned image.")
    y: int | None = Field(None, ge=0, strict=True, description="Y pixel in the returned image.")
    text: str | None = Field(None, max_length=512, description="Plain text to type into the focused control.")
    steps: int | None = Field(None, ge=-8, le=8, strict=True, description="Wheel notches; positive scrolls up.")
    key: Literal["enter", "tab", "escape", "backspace", "delete", "left", "up", "right", "down", "home", "end"] | None = None


POINTER = {"move", "click", "scroll"}


async def native(request, abort=None):
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-B", str(Path(__file__).with_name("windows.py")),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    io = asyncio.create_task(process.communicate(json.dumps(request).encode() + b"\n"))
    stop = asyncio.create_task(abort.wait()) if abort is not None else None
    try:
        done, _ = await asyncio.wait({io, stop} if stop else {io}, timeout=8,
                                     return_when=asyncio.FIRST_COMPLETED)
        if stop in done:
            raise asyncio.CancelledError
        if io not in done:
            raise OSError("Desktop operation timed out. Observe the window before any further input.")
        output, _ = io.result()
    finally:
        if stop:
            stop.cancel()
        if not io.done() and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await asyncio.gather(io, *([stop] if stop else []), return_exceptions=True)
    if process.returncode:
        raise OSError("Windows desktop helper failed. Observe the window before any further input.")
    result = json.loads(output)
    if result.get("error"):
        raise ValueError(result["error"])
    return result


class ComputerUseTool(BaseAgenticTool):
    name = "computer_use_tool"
    description = (
        "Operate an approved local Windows window with screenshots and the system mouse/keyboard. "
        "List windows, observe one by ID, then use its latest screenshot ID for one input action. "
        "Ask the user to foreground the target when needed; observe again after each action to verify its effect."
    )
    input_schema = ComputerInput

    def __init__(self):
        self.reset()
        self._lock = asyncio.Lock()

    def reset(self, _event=None):
        self._windows = {}
        self._target = None
        self._snapshot = None

    def unavailable(self):
        if not enabled():
            return "Computer Use is disabled or unavailable on this host."
        if self.context.get("agent_depth", 0) or self.context.get("delegation", {}).get("authority_required"):
            return "Computer Use is limited to the main local agent."
        abort = self.context.get("abort_event")
        if abort is not None and abort.is_set():
            return "The task stopped. No new desktop input was sent."
        return ""

    def is_read_only_call(self, **kwargs):
        return kwargs.get("action") in {"list", "observe"}

    def is_concurrency_safe_call(self, **kwargs):
        return False

    def check_permissions(self, request):
        blocked = self.unavailable()
        if blocked:
            return {"behavior": "deny", "reason": blocked}
        params = request["input_payload"]
        if params["action"] == "observe" and params.get("window") != self._target:
            return {"behavior": "ask", "reason": "Approve screenshots and mouse/keyboard input for this window in the current task."}
        return None if params["action"] in {"list", "observe"} else {"behavior": "allow"}

    def build_permission_summary(self, action, window=None, **_):
        if window in self._windows:
            target = self._windows[window]
            return (f"Windows Computer Use: {target['title']} (PID {target['pid']})\n"
                    "Screenshots may be sent to the selected model. Input uses the real mouse and keyboard.")
        return f"Windows Computer Use: {action}"

    def validate_input(self, action, window=None, snapshot=None, x=None, y=None, text=None, steps=None, key=None):
        if action == "observe" and window not in self._windows:
            return "Observe requires a window ID from the latest list."
        if action in POINTER and (x is None or y is None):
            return "This action requires both screenshot pixel coordinates."
        if action == "scroll" and not steps:
            return "Scroll requires nonzero steps."
        if action == "type" and (not text or not text.isprintable()):
            return "Type requires printable text. Use key for Enter or Tab."
        if action == "key" and not key:
            return "Key requires a named key."
        if action not in {"list", "observe"} and not snapshot:
            return "Input requires the latest screenshot ID."
        return None

    async def execute(self, action, window=None, snapshot=None, **params):
        async with self._lock:
            try:
                error = self.unavailable()
                if error:
                    raise ValueError(error)
                confinement = self.context.get("execution_sandbox")
                if confinement:
                    confinement.check_tool(self.name)
                return await self._perform(action, window, snapshot, params)
            except asyncio.CancelledError:
                self.reset()
                raise
            except (ValueError, OSError, SandboxUnavailable) as error:
                self._snapshot = None
                return build_tool_result(ok=False, content=str(error), is_error=True)

    async def _perform(self, action, window, snapshot, params):
        abort = self.context.get("abort_event")
        if action == "list":
            self.reset()
            listed = await native({"action": "list"}, abort)
            self._windows = {uuid.uuid4().hex[:12]: item for item in listed["windows"]}
            if not self._windows:
                return build_tool_result(ok=True, content="No available windows. Ask the user to open the target application.")
            return build_tool_result(ok=True, content=json.dumps(
                [{"window": identity, "title": item["title"]} for identity, item in self._windows.items()],
                ensure_ascii=False))
        if action == "observe":
            self._snapshot = None
            if window not in self._windows:
                raise ValueError("List windows again before observing this target.")
            try:
                contract = self.context.get("model_input_contract")
                model = self.context.get("request_model", self.context.get("primary_model"))
                inputs = contract(model) if callable(contract) else None
                validate_media([{"type": "image"}], inputs)
            except UnsupportedInput:
                raise ValueError("Choose a model with image input to use desktop screenshots.") from None
            image = await native({"action": "observe", "target": self._windows[window]}, abort)
            token = uuid.uuid4().hex
            self._target = window
            self._snapshot = (token, time.monotonic(), image["bounds"], image["size"], image["focus"])
            return build_tool_result(ok=True, content=[
                {"type": "text", "text": f"Snapshot {token}; image size {image['size'][0]} × {image['size'][1]}. "
                 "Use these image pixels for one action, then observe again."},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image["png"]}},
            ], metadata={"snapshot": token, "width": image["size"][0], "height": image["size"][1]})
        previous, self._snapshot = self._snapshot, None
        if previous is None or previous[0] != snapshot or time.monotonic() - previous[1] > 180:
            raise ValueError("This screenshot is stale or was already used. Observe the selected window again.")
        await native({"action": action, "target": self._windows[self._target],
                      "bounds": previous[2], "size": previous[3], "focus": previous[4], **params}, abort)
        return build_tool_result(ok=True, content="Input sent. Observe the selected window again to verify its effect.")
