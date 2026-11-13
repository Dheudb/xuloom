"""Keep a prompt below a running turn's output so the user can add instructions or stop."""
import asyncio
from contextlib import ExitStack, asynccontextmanager

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

from xuloom.core.run_result import PersistenceError

PROMPT = "↳ "


class LiveInput:
    """Queue typed lines through ``engine.queue_input`` while a turn runs; Ctrl+C stops the turn.

    Blocking dialogs (approvals, questions) must run inside :meth:`paused`, which hands the
    terminal back and restores the unfinished draft afterwards.
    """

    def __init__(self, console, status=lambda: "", **session_options):
        self.console = console
        self.session = PromptSession(**session_options)
        self.status = status
        self.engine = None
        self.stopped = False
        self._task = None
        self._abort_task = None
        self._stdout = None
        self._draft = ""
        self._lock = asyncio.Lock()

    @property
    def active(self):
        return self.engine is not None

    @asynccontextmanager
    async def running(self, engine):
        self.engine, self.stopped, self._draft = engine, False, ""
        self._resume()
        try:
            yield self
        finally:
            try:
                await self._suspend()
            finally:
                abort_task, self._abort_task = self._abort_task, None
                try:
                    if abort_task is not None:
                        await abort_task
                finally:
                    self.engine = None

    @asynccontextmanager
    async def paused(self):
        if not self.active:
            yield
            return
        async with self._lock:
            await self._suspend()
            try:
                yield
            finally:
                if self.active and not self.stopped:
                    self._resume()

    def _resume(self):
        self._stdout = ExitStack()
        self._stdout.enter_context(patch_stdout(raw=True))
        self._task = asyncio.create_task(self._read())

    async def _suspend(self):
        task, self._task = self._task, None
        if task is not None and not task.done():
            self._draft = self.session.default_buffer.text
            if self.session.app.is_running:
                self.session.app.exit(result=None)
            else:
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._stdout is not None:
            self._stdout.close()
            self._stdout = None

    async def _read(self):
        while self.active:
            draft, self._draft = self._draft, ""
            try:
                text = await self.session.prompt_async(PROMPT, default=draft, bottom_toolbar=self._toolbar,
                                                       refresh_interval=0.5)
            except KeyboardInterrupt:
                self.stop()
                return
            except EOFError:
                continue
            if text is None:
                return
            self.submit(text)

    def _toolbar(self):
        hint = "Enter 追加指令 · Ctrl+C 停止"
        status = self.status()
        return f" {status} │ {hint}" if status else " " + hint

    def submit(self, text):
        """Queue one typed line; report the outcome above the prompt."""
        text = text.strip()
        if not text:
            return None
        if text.startswith("/"):
            self.console.print("任务运行中只能追加文字说明；斜杠命令请在本轮结束后输入。", markup=False)
            return None
        try:
            receipt = self.engine.queue_input(text)
        except (ValueError, PersistenceError) as error:
            self.console.print("补充要求未加入：" + str(error), markup=False)
            return None
        self.console.print("已加入补充要求，将在当前步骤结束后送达。", style="dim", markup=False)
        return receipt

    def stop(self):
        if self.stopped:
            return
        self.stopped = True
        self.console.print("正在停止当前任务…", style="yellow", markup=False)
        self._abort_task = asyncio.create_task(self.engine.request_abort())
