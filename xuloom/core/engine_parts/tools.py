"""Tool registration, activity events, batching, hooks and worker collection."""
import asyncio
import copy
import inspect
import json
import logging
import re
import time
import uuid
from xuloom.core.messages import create_user_message
from xuloom.core.model_stream import AbortRequestedError
from xuloom.core.run_result import PersistenceError
from xuloom.core.session_lock import SessionBusyError, SessionConflictError
from xuloom.core.tool_execution import execute_tool, execute_tool_batches

_logger = logging.getLogger("xuloom.core.engine")


class ToolOrchestrationMixin:
    """Tool registration, activity events, batching, hooks and worker collection."""

    def activate_tools(self, tool_names):
        activated = []
        for name in tool_names or []:
            accept = self.tool_context.get("activate_tool_filter")
            if accept and not accept(name, self.latent_tools.get(name) or self.available_tools.get(name)):
                self.activated_tools.discard(name)
                continue
            if self.execution_sandbox.enabled:
                from xuloom.security.execution import FILE_TOOLS, CONTROL_TOOLS, PROCESS_TOOLS
                if name not in FILE_TOOLS | CONTROL_TOOLS | PROCESS_TOOLS:
                    continue
            tool = self.latent_tools.pop(name, None)
            if tool is None and name in self.available_tools:
                tool = self.available_tools[name]
            if not tool:
                continue
            tool.context = self.tool_context
            self.available_tools[name] = tool
            self.activated_tools.add(name)
            activated.append(name)

        self.tool_context["engine_available_tools"] = self.available_tools
        self.tool_context["latent_tools_registry"] = self.latent_tools
        return activated

    def register_mcp_tools(self, tools):
        accept = self.tool_context.get("activate_tool_filter")
        if accept:
            eligible = {name: tool for name, tool in tools.items() if accept(name, tool)}
            self.activated_tools.difference_update(tools.keys() - eligible.keys())
            tools = eligible
        if set(tools).intersection(self.available_tools.keys() | self.latent_tools.keys()):
            raise ValueError("MCP tool conflicts with an existing tool")
        for name, tool in tools.items():
            tool.context = self.tool_context
            self.latent_tools[name] = tool
        self.activate_tools(self.activated_tools.intersection(tools))
        return set(tools)

    def register_tool(self, tool):
        """Register one trusted Python tool before a turn; never replace native tools."""
        from xuloom.tools.base import BaseAgenticTool
        self._assert_open()
        if self.has_active_turn():
            raise ValueError("Register tools while the engine is idle")
        if not isinstance(tool, BaseAgenticTool):
            raise TypeError("Custom tools must implement BaseAgenticTool")
        definition = tool.get_tool_definition()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", definition["name"]):
            raise ValueError("Invalid custom tool name")
        if tool.name in self.available_tools or tool.name in self.latent_tools:
            raise ValueError("Tool name already registered: " + tool.name)
        tool.context = self.tool_context
        self.available_tools[tool.name] = tool
        self.activated_tools.add(tool.name)
        return tool.name

    def set_event_handler(self, handler):
        """Set the existing synchronous capability listener; streaming uses submit_message."""
        self._assert_open()
        if self.has_active_turn():
            raise ValueError("Set the event handler while the engine is idle")
        if handler is None:
            self.tool_context.pop("capability_event", None)
            return
        if not callable(handler) or inspect.iscoroutinefunction(handler):
            raise TypeError("Capability event handlers must be synchronous callables")
        def notify(event):
            try:
                handler(copy.deepcopy(event))
            except Exception as error:
                _logger.warning("Capability listener failed (%s); execution receipts remain authoritative", type(error).__name__)
        self.tool_context["capability_event"] = notify

    def _record_tool_start(self, tool_name: str, tool_use_id: str, tool_inputs: dict):
        self.in_progress_tools[tool_use_id] = {
            "tool_name": tool_name,
            "tool_use_id": tool_use_id,
            "status": "running",
            "input_preview": str(tool_inputs)[:220],
        }
        self._sync_message_context()
        self._emit_tool_activity("tool_started", tool_name, tool_use_id, input=copy.deepcopy(tool_inputs))

    def _emit_tool_activity(self, kind, tool_name, tool_use_id, **payload):
        callback = self.tool_context.get("capability_event")
        if callback:
            callback({"type": kind, "tool_name": tool_name, "tool_use_id": tool_use_id,
                      "agent_id": self.agent_id if self.agent_depth else None,
                      "agent_role": self.agent_role, "timestamp": int(time.time() * 1000), **payload})

    MAX_TOOL_CONCURRENCY = 10
    SIBLING_CANCEL_TOOLS = frozenset({"bash_tool"})

    def _is_concurrency_safe(self, tool_name: str, tool_inputs: dict) -> bool:
        if "__xuloom_continuation" in tool_inputs:
            # Register the complete declaration before the root can stream-execute.
            return False
        handler = self.available_tools.get(tool_name)
        if not handler:
            return False

        inspector = getattr(handler, "is_concurrency_safe_call", None)
        if callable(inspector):
            try:
                return bool(inspector(**tool_inputs))
            except Exception:
                return False

        return False

    @staticmethod
    def _tb_get(tool_block, key, default=""):
        if isinstance(tool_block, dict):
            return tool_block.get(key, default)
        return getattr(tool_block, key, default)

    def _partition_tool_batches(self, tool_calls):
        batches = []
        for tool_block in tool_calls:
            name = self._tb_get(tool_block, "name", "")
            inp = self._tb_get(tool_block, "input", {}) or {}
            is_safe = self._is_concurrency_safe(name, inp)
            if is_safe and batches and batches[-1]["is_concurrency_safe"]:
                batches[-1]["blocks"].append(tool_block)
            else:
                batches.append({
                    "is_concurrency_safe": is_safe,
                    "blocks": [tool_block],
                })
        return batches

    async def _execute_tool_wrapper(self, tool_block, sys_print_callback=print, *, turn: int = 0, event_callback=None, result_callback=None):
        return await execute_tool(self, tool_block, sys_print_callback, turn=turn, event_callback=event_callback, result_callback=result_callback)

    async def agents(self, action="list", **parameters):
        """Versioned host API over the existing authorized agent tool and execution journal."""
        identity = "host-" + uuid.uuid4().hex
        captured = []
        inputs = {**parameters, "action": action, "response_format": "json"}
        # Keep the versioned host API's blocking default; model calls launch in
        # the background unless explicitly requested otherwise.
        inputs.setdefault("run_in_background", False)
        try:
            self.require_writer()
            receipt = await self._execute_tool_wrapper({"id": identity, "name": "agent_tool", "input": inputs},
                lambda _: None, result_callback=captured.append)
            if receipt["is_error"]:
                return {"api_version": 1, "ok": False, "session_id": self.session_id, "tool_use_id": identity,
                    "error": {"code": "tool_error", "message": receipt["content"]}, "data": None}
            data = json.loads(captured[0])
            return {"api_version": 1, "ok": True, "session_id": self.session_id, "tool_use_id": identity, "data": data}
        except (PersistenceError, SessionBusyError, SessionConflictError) as error:
            return {"api_version": 1, "ok": False, "session_id": self.session_id, "tool_use_id": identity,
                "error": {"code": "persistence_error" if isinstance(error, PersistenceError) else "session_busy" if isinstance(error, SessionBusyError) else "session_conflict", "message": str(error)}, "data": None}

    async def _execute_tool_batches(self, tool_calls, sys_print_callback=print, *, turn: int = 0, event_callback=None):
        return await execute_tool_batches(self, tool_calls, sys_print_callback, turn=turn, event_callback=event_callback)

    def _run_hooks(self, event: str, payload: dict) -> list:
        try:
            return self.hook_manager.execute(event, payload)
        except Exception:
            return []

    def _append_hook_messages(self, event: str, outputs: list):
        if not outputs:
            return
        for index, content in enumerate(outputs, start=1):
            self.messages.append(create_user_message(
                f"<system-reminder>\n"
                f"[Hook:{event} #{index}]\n"
                f"{content}\n"
                f"</system-reminder>",
                is_meta=True,
                origin="hook",
            ))
        self._sync_message_context()

    async def _await_gate_and_execute_tool(
        self, gate, tool_block, sys_print_callback=print, *,
        turn: int = 0, event_callback=None,
        semaphore: asyncio.Semaphore = None,
        sibling_cancel: asyncio.Event = None,
    ):
        if gate is not None:
            await gate
        if self.abort_event.is_set():
            raise AbortRequestedError("Abort requested before tool execution.")
        if sibling_cancel is not None and sibling_cancel.is_set():
            tool_id = getattr(tool_block, "id", "") or (tool_block.get("id", "") if isinstance(tool_block, dict) else "")
            return {
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": "Cancelled: sibling tool in batch failed.",
                "is_error": True,
            }

        async def _do_execute():
            res = await self._execute_tool_wrapper(
                tool_block, sys_print_callback, turn=turn, event_callback=event_callback,
            )
            if (
                res.get("is_error")
                and sibling_cancel is not None
                and (getattr(tool_block, "name", "") or (tool_block.get("name", "") if isinstance(tool_block, dict) else ""))
                in self.SIBLING_CANCEL_TOOLS
            ):
                sibling_cancel.set()
            return res

        if semaphore is not None:
            async with semaphore:
                return await _do_execute()
        return await _do_execute()

    async def _cancel_streaming_tool_tasks(self, entries: list):
        pending = [
            item.get("task")
            for item in entries or []
            if item.get("task") is not None and not item["task"].done()
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


    async def _collect_subagents_before_final(self, *, turn, event_callback=None):
        """Close the completion race without replaying work or consuming model turns while waiting."""
        workers = self.tool_context["subagent_tasks"]
        active = lambda: {key: task for key, task in workers.tasks.items() if not task.done()}
        pending = active()
        waited = bool(pending)
        if pending:
            stopped = asyncio.create_task(self.abort_event.wait())
            input_ready = asyncio.create_task(self.user_input_event.wait())
            try:
                while pending:
                    if self.user_inbox: return True
                    if input_ready.done(): input_ready = asyncio.create_task(self.user_input_event.wait())
                    if self.abort_event.is_set():
                        raise AbortRequestedError("Abort requested while waiting for subagents.")
                    await self._emit_stream_event(event_callback, {"type": "subagents_waiting", "count": len(pending),
                        "agent_ids": list(pending), "message": f"等待 {len(pending)} 个子代理返回结果，全部结束后再汇总。"})
                    await asyncio.wait([*pending.values(), stopped, input_ready], return_when=asyncio.FIRST_COMPLETED)
                    if self.abort_event.is_set():
                        raise AbortRequestedError("Abort requested while waiting for subagents.")
                    pending = active()
            finally:
                stopped.cancel()
                input_ready.cancel()
                await asyncio.gather(stopped, input_ready, return_exceptions=True)
        # Include workers that finished while the model was streaming its answer,
        # even if no worker remains active by the time this gate is reached.
        reports = workers.completed()
        if not waited and not reports:
            return False
        for report in reports:
            self.messages.append(create_user_message("Background subagent completed: " + json.dumps(report, ensure_ascii=False), origin="subagent"))
        self.messages.append(create_user_message(
            "\n\n[Delegation completion guard]\nThe previous draft was provisional. All owned subagent tasks have now ended, "
            "and any newly returned results are included above. Continue the requested workflow and synthesize the actual results. "
            "Distinguish completed, crashed, cancelled and missing-output workers. Do not invent their output or claim that all succeeded. "
            "Do not relaunch or repeat completed work just to prepare the final answer.",
            is_meta=True, origin="delegation_completion"))
        self._sync_message_context()
        await self._record_loop_transition("delegation_completion_guard", turn=turn, event_callback=event_callback,
            waited=waited, report_count=len(reports))
        await self._emit_stream_event(event_callback, {"type": "subagents_waiting", "count": 0,
            "message": "子代理均已结束，正在汇总实际结果；失败或取消的任务会单独说明。"})
        self.persist_session_state()
        return True
