import asyncio
import io
import unittest

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from xuloom.live_input import LiveInput


class FakeEngine:
    def __init__(self, refuse=None):
        self.queued, self.aborted, self.refuse = [], 0, refuse

    def queue_input(self, text):
        if self.refuse:
            raise ValueError(self.refuse)
        self.queued.append(text)
        return {"id": str(len(self.queued)), "text": text, "status": "queued"}

    async def request_abort(self):
        self.aborted += 1
        return True


async def until(condition, message):
    for _ in range(300):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(message)


class LiveInputTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.out = io.StringIO()
        self.console = Console(file=self.out, force_terminal=False, width=200)
        self.pipes = create_pipe_input()
        self.pipe = self.pipes.__enter__()
        self.addCleanup(self.pipes.__exit__, None, None, None)
        session = create_app_session(input=self.pipe, output=DummyOutput())
        session.__enter__()
        self.addCleanup(session.__exit__, None, None, None)

    def live(self):
        return LiveInput(self.console)

    async def test_lines_typed_during_a_turn_are_queued_and_commands_refused(self):
        engine, pipe = FakeEngine(), self.pipe
        live = self.live()
        async with live.running(engine):
            pipe.send_text("prefer the second approach\r")
            await until(lambda: engine.queued, "typed line was not queued")
            pipe.send_text("/model\r   \r")
            await until(lambda: "斜杠命令" in self.out.getvalue(), "slash command was not refused")
        self.assertFalse(live.active)
        self.assertEqual(engine.queued, ["prefer the second approach"])
        self.assertIn("已加入补充要求", self.out.getvalue())
        self.assertFalse(live.stopped)

    async def test_refused_input_is_reported_without_stopping(self):
        engine, pipe = FakeEngine(refuse="No running task accepts supplemental input"), self.pipe
        live = self.live()
        async with live.running(engine):
            pipe.send_text("late\r")
            await until(lambda: "未加入" in self.out.getvalue(), "refusal was not reported")
        self.assertIn("No running task accepts supplemental input", self.out.getvalue())
        self.assertEqual(engine.aborted, 0)

    async def test_ctrl_c_stops_the_turn_once(self):
        engine, pipe = FakeEngine(), self.pipe
        live = self.live()
        async with live.running(engine):
            pipe.send_text("\x03")
            await until(lambda: engine.aborted, "Ctrl+C did not stop the turn")
        self.assertTrue(live.stopped)
        self.assertEqual(engine.aborted, 1)

    async def test_pause_hands_over_the_terminal_and_keeps_the_draft(self):
        engine, pipe = FakeEngine(), self.pipe
        live = self.live()
        async with live.running(engine):
            pipe.send_text("half typed")
            await until(lambda: live.session.default_buffer.text == "half typed", "draft not typed")
            async with live.paused():
                self.assertIsNone(live._task)
            await until(lambda: live.session.default_buffer.text == "half typed", "draft was not restored")
            pipe.send_text(" and finished\r")
            await until(lambda: engine.queued, "resumed draft was not queued")
        self.assertEqual(engine.queued, ["half typed and finished"])

    async def test_pause_outside_a_turn_is_a_no_op(self):
        live = self.live()
        async with live.paused():
            self.assertFalse(live.active)

    def test_live_tui_leaves_the_prompt_area_alone_and_reports_delivery(self):
        from xuloom.main import InteractiveTerminalTUI

        class Engine:
            session_cwd = "."
            def get_mode(self): return "normal"

        tui = InteractiveTerminalTUI(self.console, lambda: Engine())
        tui._stream = raw = io.StringIO()
        tui.start_streaming(live=True)
        for event in ({"type": "message_start", "turn": 1}, {"type": "text_delta", "text": "partial"},
                      {"type": "tool_scheduled", "tool_name": "file_read_tool"},
                      {"type": "user_input_applied", "inputs": [{"id": "1", "text": "use option B"}]},
                      {"type": "message_stop", "stop_reason": "end_turn"}):
            tui.track_stream_event(event)
        tui.stop_streaming()
        self.assertEqual(raw.getvalue(), "")
        self.assertIn("补充要求已送达：use option B", self.out.getvalue())
        self.assertIn("Tools: 1", tui.status_text())
        self.assertFalse(tui.live)

    async def test_typed_instruction_reaches_the_running_engine(self):
        import os
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from xuloom import QueryEngine
        from xuloom.testing import ScriptedModel
        from xuloom.testing.models import model_response

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / "work").mkdir()
        env = patch.dict(os.environ, {"XULOOM_DATA_DIR": str(root / "data"), "XULOOM_CONFIG_DIR": str(root / "config")})
        env.start()
        self.addCleanup(env.stop)
        started = asyncio.Event()

        async def first(request):
            started.set()
            await until(lambda: engine.user_inbox, "instruction was not queued during the model response")
            return model_response(text="first draft")

        model = ScriptedModel([first, model_response(text="revised")])
        live = self.live()
        with model:
            async with QueryEngine(model.config(cwd=str(root / "work"))) as engine:
                async with live.running(engine):
                    events = []

                    async def turn():
                        async for event in engine.submit_message("ORIGINAL", sys_print_callback=lambda _: None):
                            events.append(event)

                    task = asyncio.create_task(turn())
                    await asyncio.wait_for(started.wait(), 10)
                    self.pipe.send_text("focus on the second option\r")
                    await asyncio.wait_for(task, 20)
        self.assertEqual(len(model.requests), 2)
        self.assertIn("focus on the second option", str(model.requests[1].messages))
        self.assertTrue(next(event for event in events if event["type"] == "final")["result"]["ok"])


if __name__ == "__main__":
    unittest.main()
