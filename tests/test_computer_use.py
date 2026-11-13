import asyncio
import json
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

from tests.test_model_adapters import AdapterFixture
from xuloom.computer_use.tool import ComputerUseTool
from xuloom.models import ToolCall, model_response
from xuloom.plugins import default_plugin_names, set_plugin_enabled


class ComputerUseTests(AdapterFixture):
    async def test_opt_in_is_latent_and_local_only(self):
        self.assertNotIn("computer-use", default_plugin_names())
        set_plugin_enabled("computer-use", True)
        self.assertIn("computer-use", default_plugin_names())
        self.assertNotIn("computer-use", default_plugin_names(bundled=False))
        with patch.dict(os.environ, {"XULOOM_REMOTE": "1"}):
            async with self.engine(load_enabled_plugins=True) as engine:
                self.assertNotIn("computer_use_tool", engine.latent_tools)
        if sys.platform == "win32":
            async with self.engine(load_enabled_plugins=True) as engine:
                self.assertIn("computer_use_tool", engine.latent_tools)
                self.assertNotIn("computer_use_tool", engine.available_tools)
                found = await engine.available_tools["tool_search_tool"](query="移动鼠标光标")
                self.assertIn("computer_use_tool [latent]", found["content"])
        set_plugin_enabled("computer-use", False)
        self.assertNotIn("computer-use", default_plugin_names())

    async def test_normal_turn_asks_even_with_full_access_and_returns_image(self):
        if sys.platform != "win32":
            self.skipTest("Windows plugin registration")
        set_plugin_enabled("computer-use", True)
        selected = {}

        async def observe(request):
            receipt = self.result_content(request, "list")
            selected["id"] = json.loads(receipt[receipt.index("[{"):])[0]["window"]
            return model_response(tool_calls=[ToolCall("observe", "computer_use_tool",
                                                      {"action": "observe", "window": selected["id"]})])

        async def click(request):
            blocks = self.result_content(request, "observe")
            self.assertTrue(any(block.get("type") == "image" for block in blocks))
            token = next(block["text"].split()[1].rstrip(";") for block in blocks
                         if block.get("text", "").startswith("Snapshot "))
            selected["token"] = token
            return model_response(tool_calls=[ToolCall("click", "computer_use_tool",
                                                      {"action": "click", "x": 20, "y": 5, "snapshot": token})])

        self.adapter.script = [
            model_response(tool_calls=[ToolCall("list", "computer_use_tool", {"action": "list"})]),
            observe, click, model_response(text="done"),
        ]
        approve = AsyncMock(return_value="allow")
        backend = AsyncMock(side_effect=[
            {"windows": [{"title": "Owned test app", "pid": 42}]},
            {"bounds": [100, 100, 50, 30], "size": [50, 30], "focus": 12,
             "png": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/xk0AAAAASUVORK5CYII="},
            {},
        ])
        with patch("xuloom.computer_use.tool.native", backend):
            async with self.engine(load_enabled_plugins=True) as engine:
                engine.set_permission_handler(approve)
                self.assertEqual(engine.activate_tools(["computer_use_tool"]), ["computer_use_tool"])
                result = await engine.run("Inspect and click the test app", sys_print_callback=lambda _: None)
                self.assertTrue(result.ok, result)
                self.assertEqual([call.args[0]["action"] for call in backend.await_args_list], ["list", "observe", "click"])
                self.assertIn("Owned test app", approve.call_args.args[0].summary)
                approve.assert_awaited_once()
                tool = engine.available_tools["computer_use_tool"]
                replay = await tool(action="click", x=20, y=5, snapshot=selected["token"])
                self.assertTrue(replay["is_error"])
                self.assertEqual(backend.await_count, 3)

    @staticmethod
    def result_content(request, call_id):
        return next(block["content"] for msg in request.messages for block in msg.get("content", [])
                    if isinstance(block, dict) and block.get("tool_use_id") == call_id)

    async def test_failed_input_consumes_snapshot_and_disable_stops_existing_tool(self):
        tool = ComputerUseTool()
        abort = asyncio.Event()
        tool.context = {"abort_event": abort}
        backend = AsyncMock(side_effect=[
            {"windows": [{"title": "Test", "pid": 42}]},
            {"bounds": [0, 0, 100, 100], "size": [100, 100], "focus": 12, "png": "fixture"},
            ValueError("Window changed"),
        ])
        with patch("xuloom.computer_use.tool.enabled", return_value=True), patch(
                "xuloom.computer_use.tool.native", backend):
            listing = await tool(action="list")
            window = json.loads(listing["content"])[0]["window"]
            observation = await tool(action="observe", window=window)
            request = {"action": "click", "snapshot": observation["metadata"]["snapshot"], "x": 5, "y": 5}
            self.assertIn("Window changed", (await tool(**request))["content"])
            self.assertIn("stale", (await tool(**request))["content"])
            abort.set()
            self.assertIn("stopped", (await tool(action="list"))["content"])
        self.assertEqual(backend.await_count, 3)

        with patch("xuloom.computer_use.tool.enabled", return_value=False), patch(
                "xuloom.computer_use.tool.native", backend):
            self.assertIn("disabled", (await tool(action="list"))["content"])
        self.assertEqual(backend.await_count, 3)


@unittest.skipUnless(sys.platform == "win32", "Windows native input")
class WindowsInputTests(unittest.TestCase):
    def test_pointer_dispatch_matches_screenshot_and_rejects_overlay(self):
        from ctypes import cast
        from xuloom.computer_use import windows
        target = {"hwnd": 15}
        request = {"action": "click", "target": target, "bounds": [100, 200, 800, 600],
                   "size": [400, 300], "x": 20, "y": 10}

        def cursor(ptr):
            point = cast(ptr, windows.c.POINTER(windows.w.POINT)).contents
            point.x, point.y = 141, 221
            return True

        with patch.object(windows, "verify_input_target"), patch.object(windows, "point_window"), patch.object(
            windows, "ancestor", side_effect=[15, 17, 15, 15]
        ), patch.object(windows, "move_pointer", return_value=True) as move, patch.object(
            windows, "cursor_position", cursor
        ), patch.object(windows, "send_input", return_value=2) as send:
            with self.assertRaisesRegex(ValueError, "pointer or window changed"):
                windows.act(request)
            send.assert_not_called()
            self.assertEqual(windows.act(request), {})
            move.assert_called_with(141, 221)
            self.assertEqual([event.data.mouse.flags for event in send.call_args.args[1]], [2, 4])

    def test_unicode_dispatch_requires_same_keyboard_focus(self):
        from xuloom.computer_use import windows
        request = {"action": "type", "target": {"hwnd": 15}, "bounds": [0, 0, 100, 100],
                   "size": [100, 100], "focus": 7, "text": "中🙂"}
        with patch.object(windows, "verify_input_target", side_effect=[8, 7, 7]), patch.object(
                windows, "send_input", return_value=6) as send:
            with self.assertRaisesRegex(ValueError, "focus changed"):
                windows.act(request)
            send.assert_not_called()
            self.assertEqual(windows.act(request), {})
            events = [event.data.keyboard for event in send.call_args.args[1]]
            self.assertEqual([event.scan for event in events[::2]], [0x4E2D, 0xD83D, 0xDE42])
            self.assertEqual([event.flags for event in events], [4, 6] * 3)
