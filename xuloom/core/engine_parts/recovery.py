"""Turn failure handling: rollback, max-token continuation, protocol repair and context overflow."""
import json
import uuid
from types import SimpleNamespace
from xuloom.core.messages import create_assistant_message, create_user_message, get_msg_uuid, rollback_incomplete_turn
from xuloom.core.model_stream import AbortRequestedError
from xuloom.core.run_result import PersistenceError, finish_run
from xuloom.core.storage import artifact_directory
from xuloom.protocols.dsml import ToolProtocolError
from xuloom.protocols.stream_watch import StreamInterrupted
from xuloom.core.engine_parts.loop import _CONTINUE
from xuloom.core.engine_state import EngineState


class TurnRecoveryMixin(EngineState):
    """Turn failure handling: rollback, max-token continuation, protocol repair and context overflow."""

    def _rollback_incomplete_tool_turn(self, assistant_message: dict) -> bool:
        if not assistant_message or not self.messages:
            return False

        target_uuid = get_msg_uuid(assistant_message)
        if target_uuid:
            from xuloom.core.messages import rollback_to_uuid
            removed = rollback_to_uuid(self.messages, target_uuid)
            if removed:
                self._sync_message_context()
                self.persist_session_state()
                return True

        if self.messages and self.messages[-1] is assistant_message:
            content = assistant_message.get("content")
            if isinstance(content, list):
                has_tool_use = any(
                    (block.get("type") if isinstance(block, dict) else getattr(block, "type", None)) == "tool_use"
                    for block in content
                )
                if has_tool_use:
                    self.messages.pop()
                    self._sync_message_context()
                    self.persist_session_state()
                    return True

        removed = rollback_incomplete_turn(self.messages)
        if removed:
            self._sync_message_context()
            self.persist_session_state()
            return True

        return False

    def _build_continuation_prompt(self) -> str:
        return (
            "<system-reminder>\n"
            "The previous assistant response stopped because the output token budget was exhausted.\n"
            "Continue immediately from where you left off. Do not repeat completed content.\n"
            "</system-reminder>"
        )

    async def _recover_max_tokens(self, streaming_tool_entries, loop_state, turn_count, sys_print_callback, event_callback):
        """Continue after a max_tokens stop, escalating the output limit once."""
        if streaming_tool_entries:
            await self._cancel_streaming_tool_tasks(streaming_tool_entries)
        loop_state["max_tokens_recovery_attempts"] += 1
        remaining_output_budget = self._remaining_output_budget(loop_state)
        if remaining_output_budget is not None and remaining_output_budget <= 0:
            await self._record_loop_transition(
                "session_output_budget_exhausted",
                turn=turn_count,
                event_callback=event_callback,
                session_output_budget=loop_state["session_output_budget"],
                output_tokens_used=self.session_token_usage.get("output_tokens", 0),
            )
            self.persist_session_state()
            return finish_run(self, "已达到已设置的输出 Token 预算，已有结果已保存。", 'budget_exhausted')
        transition_reason = "max_output_tokens_recovery"
        if (
            loop_state["max_tokens_recovery_attempts"] >= 2
            and loop_state["max_tokens_escalations"] < 1
        ):
            loop_state["max_tokens"] = 64000
            loop_state["max_tokens_escalations"] += 1
            transition_reason = "max_output_tokens_escalate"
        if remaining_output_budget is not None:
            loop_state["max_tokens"] = min(loop_state["max_tokens"], remaining_output_budget)
        self.messages.append(create_user_message(
            self._build_continuation_prompt(),
            is_continuation=True,
            origin="system_continuation",
        ))
        self._sync_message_context()
        self.persist_session_state()
        await self._record_loop_transition(
            transition_reason,
            turn=turn_count,
            event_callback=event_callback,
            max_tokens=loop_state["max_tokens"],
            recovery_attempts=loop_state["max_tokens_recovery_attempts"],
            remaining_output_budget=remaining_output_budget,
        )
        sys_print_callback(
            f"[dim yellow]↳ Continuing after max_tokens stop ({transition_reason}, budget={loop_state['max_tokens']}).[/dim yellow]"
        )
        return _CONTINUE

    async def _handle_turn_exception(self, e, assistant_message, request, tools_schema, loop_state, counters,
                                     turn_count, sys_print_callback, event_callback):
        """Map a turn failure to recovery (`_CONTINUE`) or a terminal RunResult."""
        await self._dispatch_hook("TurnError", self._hook_payload(
            "TurnError", turn=turn_count, error_type=type(e).__name__,
            cancelled=isinstance(e, AbortRequestedError)))
        from xuloom.agents.external.runtime import ExternalExecutionError
        from xuloom.core.output_limits import ModelBudgetError
        from xuloom.observability.cost import CostLimitError
        cause = e
        while cause is not None and not isinstance(cause, CostLimitError):
            cause = cause.__cause__ or cause.__context__
        if cause is not None:  # e.g. raised by compaction, possibly wrapped
            return await self._stop_for_cost(str(cause), turn_count, event_callback)
        if isinstance(e, (ExternalExecutionError, ModelBudgetError)):
            return finish_run(self, str(e), e.reason if isinstance(e, ExternalExecutionError) else "budget_exhausted")
        if isinstance(e, ToolProtocolError):
            return await self._repair_tool_protocol(e, request, tools_schema, loop_state, counters,
                                                    turn_count, sys_print_callback, event_callback)
        if isinstance(e, AbortRequestedError):
            rolled_back = self._rollback_incomplete_tool_turn(assistant_message)
            await self._record_loop_transition(
                "user_abort",
                turn=turn_count,
                event_callback=event_callback,
                rolled_back=rolled_back,
            )
            self.active_stream = None
            self.active_streaming_tool_entries = []
            self.persist_session_state()
            return finish_run(self, ("[bold yellow]Turn aborted.[/bold yellow] "
                "The current agent turn was interrupted and cleanup has completed."), 'cancelled')
        if isinstance(e, StreamInterrupted):
            partial = create_assistant_message(e.partial_text or str(e), model=loop_state["active_model"], stop_reason="stream_interrupted")
            if not e.partial_text:
                partial["_is_meta"] = True
            self.messages.append(partial)
            if e.tool_results:
                self.messages.append(create_user_message("[Interrupted stream: already executed tool results; inspect before continuing, do not replay]\n" +
                    json.dumps(e.tool_results, ensure_ascii=False, default=str), is_meta=True, origin="stream_interrupted"))
            self._sync_message_context()
            self.persist_session_state()
            return finish_run(self, ("[bold red]API Loop Exception:[/bold red] " + str(e)), 'stream_interrupted')
        if isinstance(e, PersistenceError):
            return finish_run(self, "Storage checkpoint failed: " + str(e), 'persistence_error')
        from xuloom.core.model_inputs import UnsupportedInput
        if isinstance(e, UnsupportedInput):
            return finish_run(self, str(e), 'unsupported_input')
        if (
            self._is_model_fallback_error(e)
            and loop_state["active_model"] != self.fallback_model
            and self.fallback_model
        ):
            self._strip_thinking_signatures_from_history()
            loop_state["active_model"] = self.fallback_model
            loop_state["fallback_count"] += 1
            await self._record_loop_transition(
                "model_fallback",
                turn=turn_count,
                event_callback=event_callback,
                fallback_model=self.fallback_model,
                error=str(e),
            )
            sys_print_callback(
                f"[dim yellow]↳ Primary model failed; retrying current turn with fallback model {self.fallback_model}.[/dim yellow]"
            )
            return _CONTINUE
        if self._is_context_overflow_error(e):
            if await self._recover_context_overflow(loop_state, turn_count, sys_print_callback, event_callback):
                return _CONTINUE
        rolled_back = self._rollback_incomplete_tool_turn(assistant_message)
        from xuloom.core.network import model_error_message
        if rolled_back:
            return finish_run(self, model_error_message(e) + " 未完成的工具调用记录已回退；已完成操作保留。", 'api_error')
        return finish_run(self, model_error_message(e), 'api_error')

    async def _repair_tool_protocol(self, e, request, tools_schema, loop_state, counters, turn_count,
                                    sys_print_callback, event_callback):
        """Feed a malformed tool call back to the model (bounded), never replaying earlier side effects."""
        # DSML is decoded atomically before dispatch. Earlier successful
        # turns and their side effects must never be rolled back/replayed.
        counters["protocol_failures"] += 1
        protocol_failures = counters["protocol_failures"]
        retrying = protocol_failures <= 2 and counters["protocol_retries"] < 6
        diagnostic = ""
        try:
            directory = artifact_directory(self.session_cwd) / self.session_id / "protocol-errors"
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / (uuid.uuid4().hex + ".json")
            target.write_text(json.dumps({"code": e.code, "detail": str(e), "tool": e.tool_name,
                "turn": turn_count, "raw_completion": e.raw_text}, ensure_ascii=False, indent=2), encoding="utf-8")
            diagnostic = str(target)
        except OSError:
            pass
        if e.usage is not None and request is not None:
            self._record_token_usage(SimpleNamespace(usage=e.usage, content=[self._make_text_block(e.raw_text)]), loop_state, turn_count,
                system_prompt=request["system_prompt"], api_messages=request["api_messages"], tools_schema=tools_schema,
                thinking_config=request["thinking_config"])
        subject = "工具调用格式"
        message = (f"{subject}错误，正在请求模型修正（{protocol_failures}/2）。已完成的操作保留。" if retrying else
                   f"{subject}多次修正失败，任务已中断。已完成的操作和文件保留，可继续当前会话。")
        await self._record_loop_transition("tool_protocol_repair" if retrying else "tool_protocol_exhausted",
            turn=turn_count, event_callback=event_callback, code=e.code, detail=str(e),
            retrying=retrying, attempt=protocol_failures, diagnostic=diagnostic)
        await self._emit_stream_event(event_callback, {"type": "tool_protocol_error", "message": message,
            "retrying": retrying, "attempt": protocol_failures, "limit": 2,
            "detail": str(e), "diagnostic": diagnostic})
        sys_print_callback(message)
        if e.visible_text.strip():
            partial = create_assistant_message(e.visible_text, model=loop_state["active_model"], stop_reason="protocol_error")
            partial["_is_meta"] = True
            self.messages.append(partial)
        repair_feedback = ("\n\n[Tool protocol error: current completion rejected before any of its calls executed]\n" + str(e) +
            "\nCorrect the tool call using the currently declared tools schema. Prefer native function/tool_calls. "
            "Arguments must be a JSON object; arrays must be actual arrays, not serialized strings. "
            "If using DSML, use only declared parameter names, each exactly once, and close every tag. "
            "Continue the user's selected skill/workflow from the last successful tool result. "
            "Do not repeat completed commands, submissions or agent launches. Do not substitute an ordinary answer for the requested workflow.")
        self.messages.append(create_user_message(repair_feedback, is_meta=True, origin="tool_protocol"))
        self._sync_message_context()
        self.persist_session_state()
        if retrying:
            counters["protocol_retries"] += 1
            return _CONTINUE
        halted = create_assistant_message(message, model=loop_state["active_model"], stop_reason="tool_protocol_error")
        halted["_is_meta"] = True
        self.messages.append(halted)
        self._sync_message_context()
        self.persist_session_state()
        return finish_run(self, ("[bold red]API Loop Exception:[/bold red] " + message), 'tool_protocol_error')

    async def _recover_context_overflow(self, loop_state, turn_count, sys_print_callback, event_callback) -> bool:
        """Withhold-then-recover a context overflow; True means retry the turn.

        Stages grow more aggressive: shrink max_tokens, normal compaction, aggressive compaction.
        """
        retries = loop_state.get("reactive_compact_retries", 0)
        # Stage 1: reduce max_tokens (zero-cost, no data loss)
        current_max = loop_state["max_tokens"]
        if retries == 0 and current_max > 4096:
            new_max = max(4096, current_max // 2)
            loop_state["max_tokens"] = new_max
            loop_state["reactive_compact_retries"] = 1
            await self._record_loop_transition(
                "reactive_withhold_stage1",
                turn=turn_count,
                event_callback=event_callback,
                old_max=current_max,
                new_max=new_max,
            )
            sys_print_callback(
                f"[dim yellow]↳ Context overflow withheld (stage 1). "
                f"Reducing max_tokens {current_max} → {new_max}.[/dim yellow]"
            )
            return True
        for stage, aggressive, label, color in ((2, False, "normal", "magenta"), (3, True, "aggressive", "magenta")):
            if retries > stage - 1:
                continue
            compacted = await self._apply_layered_compaction_if_needed(
                sys_print_callback,
                force=True,
                aggressive=aggressive,
                event_callback=event_callback,
            )
            if compacted:
                loop_state["reactive_compact_retries"] = stage
                loop_state["max_tokens"] = self._get_max_output_tokens()
                await self._record_loop_transition(
                    f"reactive_withhold_stage{stage}",
                    turn=turn_count,
                    event_callback=event_callback,
                    retries=stage,
                    stage=label,
                    max_tokens_restored=loop_state["max_tokens"],
                )
                sys_print_callback(
                    f"[dim {color}]↳ Context overflow withheld (stage {stage}). "
                    f"Retrying after {label} compaction + state restore.[/dim {color}]"
                )
                return True
        return False
