"""Bound model streams by useful progress, rather than socket keepalives."""
import asyncio
import math
import os
import time

from xuloom.core.config import load_config


class StreamAborted(Exception):
    pass


class StreamInterrupted(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.partial_text = ""
        self.tool_results = []


def stream_limits():
    config = load_config(decrypt_secrets=False)
    def limit(name, default):
        try:
            value = float(os.environ.get("XULOOM_" + name.upper(), config.get(name, default)))
            return value if math.isfinite(value) and 0 < value <= 3600 else default
        except (ValueError, TypeError):
            return default
    return (limit("stream_first_response_timeout_sec", 480),
            limit("stream_idle_timeout_sec", 60), limit("stream_end_grace_sec", 1))


class StreamWatch:
    def __init__(self, abort_event, emit):
        self.abort = abort_event
        self.emit = emit
        self.first_limit, self.idle_limit, self.end_grace = stream_limits()
        self.started = time.monotonic()
        self.slow_at = self.started + min(30, self.first_limit / 2)
        self.slow_reported = False
        self.last_progress = None
        self.terminal_at = None
        self.last_emit = 0
        self.last_status = ""
        self.metrics = {"startedAt": int(time.time() * 1000), "chunks": 0, "textChars": 0, "reasoningChunks": 0,
                        "toolChunks": 0, "status": "waiting"}

    def ms(self):
        return int((time.monotonic() - self.started) * 1000)

    async def status(self, status, force=False):
        now = time.monotonic()
        if force or status != self.last_status or now - self.last_emit >= 5:
            self.metrics.update(status=status, elapsedMs=self.ms())
            await self.emit(dict(self.metrics))
            self.last_emit, self.last_status = now, status

    async def wait(self, awaitable):
        deadline = (self.terminal_at + self.end_grace if self.terminal_at is not None else
                    self.last_progress + self.idle_limit if self.last_progress is not None else self.started + self.first_limit)
        task, stopped = asyncio.ensure_future(awaitable), asyncio.create_task(self.abort.wait())
        try:
            while not self.abort.is_set():
                now = time.monotonic()
                if task.done():
                    return task.result()
                if now >= deadline:
                    break
                slow_pending = self.last_progress is None and self.terminal_at is None and not self.slow_reported
                next_check = min(self.slow_at, deadline) if slow_pending else deadline
                done, _ = await asyncio.wait({task, stopped}, timeout=max(0, next_check - now),
                                             return_when=asyncio.FIRST_COMPLETED)
                if self.abort.is_set():
                    break
                if task in done:
                    return task.result()
                if slow_pending and self.slow_at <= time.monotonic() < deadline:
                    self.slow_reported = True
                    await self.status("slow_response")
            if self.abort.is_set():
                raise StreamAborted()
            if self.terminal_at is not None:
                raise StopAsyncIteration()
            if self.last_progress is None:
                raise StreamInterrupted(f"等待模型首个有效响应超过 {self.first_limit:g} 秒，已停止等待。可以稍后重试或切换模型。")
            raise StreamInterrupted(f"模型流已连续 {self.idle_limit:g} 秒没有新的文本、推理或工具参数，连接已中断。已收到的内容和已完成的操作保留。")
        finally:
            for pending in (task, stopped):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(task, stopped, return_exceptions=True)

    async def connected(self):
        self.metrics["connectedMs"] = self.ms()
        await self.status("slow_response" if self.slow_reported else "connected", force=True)

    def request_options(self):
        # The SDK defaults to a 600-second socket read timeout. Allow longer explicit limits.
        read_limit = max(self.first_limit, self.idle_limit, self.end_grace)
        if read_limit <= 600:
            return {}
        import httpx
        return {"timeout": httpx.Timeout(read_limit, connect=10, write=600, pool=600)}

    async def chunk(self, chunk):
        self.metrics["chunks"] += 1
        self.metrics.setdefault("firstChunkMs", self.ms())
        choices = list(getattr(chunk, "choices", []) or [])
        if not choices:
            return
        choice = choices[0]
        delta = getattr(choice, "delta", None)
        text = getattr(delta, "content", None) if delta else None
        reasoning = (getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)) if delta else None
        tools = getattr(delta, "tool_calls", None) if delta else None
        finish = getattr(choice, "finish_reason", None)
        if text or reasoning or tools or finish:
            self.last_progress = time.monotonic()
            self.metrics["lastProgressMs"] = self.ms()
        if text:
            self.metrics["textChars"] += len(text)
        if reasoning:
            self.metrics["reasoningChunks"] += 1  # Count only; never expose/store private reasoning text.
        if tools:
            self.metrics["toolChunks"] += 1
        if finish:
            self.terminal_at = self.terminal_at or time.monotonic()
            self.metrics["finishReason"] = finish
            await self.status("finishing")
        elif text or tools:
            await self.status("receiving")
        elif reasoning:
            await self.status("reasoning")

    async def visible(self):
        if "firstTextMs" not in self.metrics:
            self.metrics["firstTextMs"] = self.ms()
            await self.status("receiving", force=True)
