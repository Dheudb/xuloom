"""Own background subagent tasks and their cancellation lifecycle."""
import asyncio
from collections import deque
import inspect


class SubagentTasks:
    def __init__(self, limit=None, queue_limit=None):
        self.limit = limit
        self.queue_limit = queue_limit
        self.tasks = {}
        self.results = {}
        self.backends = {}  # Optional transport handles; admission still uses this FIFO.
        self.delivered = set()
        self.running = set()
        self.pending = deque()
        self._condition = None
        self._closing = False

    def check_capacity(self, count):
        if self._closing:
            raise ValueError("Subagent task owner is closing; new tasks are not accepted")
        if self.limit is not None and self.limit <= 0:
            raise ValueError("Subagent execution is disabled")
        outstanding = sum(not task.done() for task in self.tasks.values())
        if self.limit is not None and self.queue_limit is not None and max(0, outstanding + count - self.limit) > self.queue_limit:
            raise ValueError("Subagent queue capacity is exhausted")

    async def _enter(self, identity):
        if self._condition is None:
            self._condition = asyncio.Condition()
        async with self._condition:
            await self._condition.wait_for(lambda: self._closing or
                bool(self.pending) and self.pending[0] == identity and (self.limit is None or len(self.running) < self.limit))
            if self._closing:
                raise asyncio.CancelledError()
            self.pending.popleft()
            self.running.add(identity)
            self._condition.notify_all()

    async def _leave(self, identity):
        if self._condition is None:
            return
        async with self._condition:
            self.running.discard(identity)
            if identity in self.pending:
                self.pending.remove(identity)
            self._condition.notify_all()

    def launch_batch(self, coroutines, on_cancelled_before_start=None):
        try:
            self.check_capacity(len(coroutines))
            if any(identity in self.tasks and not self.tasks[identity].done() for identity in coroutines):
                raise ValueError("Subagent is already running")
        except ValueError:
            for coroutine in coroutines.values():
                coroutine.close()
            raise
        # No awaits: capacity validation and allocation are atomic on this loop.
        for agent_id, coroutine in coroutines.items():
            self.launch(agent_id, coroutine, on_cancelled_before_start)

    def launch(self, agent_id, coroutine, on_cancelled_before_start=None):
        if agent_id in self.tasks and not self.tasks[agent_id].done():
            coroutine.close()
            raise ValueError("Subagent is already running")
        try:
            self.check_capacity(1)
        except ValueError:
            coroutine.close()
            raise

        self.results.pop(agent_id, None)
        self.delivered.discard(agent_id)
        self.pending.append(agent_id)
        started = False
        entered = False
        notified = False
        async def cancelled():
            nonlocal notified
            if not entered:
                coroutine.close()
                if on_cancelled_before_start and not notified:
                    notified = True
                    value = on_cancelled_before_start(self.results[agent_id])
                    if inspect.isawaitable(value):
                        await value
        async def run():
            nonlocal started, entered
            started = True
            try:
                await self._enter(agent_id)
                entered = True
                result = await coroutine
                self.results[agent_id] = result
                return result
            except asyncio.CancelledError:
                self.results[agent_id] = {"agent_id": agent_id, "status": "cancelled", "result": "Cancelled by user"}
                await cancelled()
                raise
            except Exception as exc:
                if not entered:
                    coroutine.close()
                self.results[agent_id] = {"agent_id": agent_id, "status": "crashed", "result": str(exc)}
                return self.results[agent_id]
            finally:
                await self._leave(agent_id)

        task = asyncio.create_task(run())
        def settled(completed):
            if completed.cancelled() and not started:
                coroutine.close()
                if agent_id in self.pending:
                    self.pending.remove(agent_id)
                self.results[agent_id] = {"agent_id": agent_id, "status": "cancelled", "result": "Cancelled before execution"}
                if on_cancelled_before_start:
                    on_cancelled_before_start(self.results[agent_id])
                if self._condition is not None and not self._closing:
                    async def wake():
                        async with self._condition:
                            self._condition.notify_all()
                    asyncio.create_task(wake())
        task.add_done_callback(settled)
        self.tasks[agent_id] = task

    async def cancel(self, agent_id):
        task = self.tasks.get(agent_id)
        if task is None:
            raise ValueError("Unknown background subagent")
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return self.results.get(agent_id, {"agent_id": agent_id, "status": "cancelled"})

    async def close(self):
        self._closing = True
        try:
            tasks = list(self.tasks.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            failures = []
            for identity, backend in list(self.backends.items()):
                try:
                    await backend.close()
                    self.backends.pop(identity, None)
                except Exception as error:
                    failures.append(error)
            if failures:
                raise failures[0]
        finally:
            self._closing = False

    def completed(self):
        results = [r for key, r in self.results.items() if key not in self.delivered]
        self.delivered.update(self.results)
        return results
