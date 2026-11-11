"""The model/tool turn loop, loop state, budgets, structured output and stop hooks."""
import asyncio
import copy
import inspect
import json
import os
import re
import sys
import uuid
from types import SimpleNamespace
from typing import Optional
from xuloom.core.messages import create_assistant_message, create_tool_result_message, create_user_message, get_msg_uuid, rollback_incomplete_turn
from xuloom.core.model_stream import AbortRequestedError
from xuloom.core.reasoning import configured_profile
from xuloom.core.run_result import PersistenceError, RunResult, finish_run
from xuloom.core.session_lock import SessionBusyError, SessionConflictError
from xuloom.core.storage import artifact_directory
from xuloom.core.tool_execution import restore_tool_activity
from xuloom.protocols.dsml import ToolProtocolError
from xuloom.protocols.stream_watch import StreamInterrupted

# Sentinel returned by loop phases: start the next loop iteration.
_CONTINUE = object()


async def _drain_cancelled(tasks):
    """Wait until cancelled tasks have finished, even if the caller is being cancelled itself."""
    waiter = asyncio.gather(*tasks, return_exceptions=True)
    try:
        await asyncio.shield(waiter)
    except asyncio.CancelledError:
        # Finish cleanup first, then let the caller's cancellation continue.
        await asyncio.wait({waiter})
        raise


class TurnLoopMixin:
    """The model/tool turn loop, loop state, budgets, structured output and stop hooks."""

    def set_structured_output_request(self, request: Optional[dict]):
        result = self.structured_output_manager.set_request(request)
        self.tool_context["structured_output_state"] = self.structured_output_manager.export_state()
        return result

    def clear_structured_output_request(self):
        self.structured_output_manager.clear()
        self.tool_context["structured_output_state"] = self.structured_output_manager.export_state()

    def _build_structured_output_feedback(self, reason: str) -> str:
        return (
            "<system-reminder>\n"
            "The previous final answer failed the structured output requirement.\n"
            "Retry and return only a valid structured response.\n"
            f"Validation failure:\n{reason}\n"
            "</system-reminder>"
        )

    async def _apply_post_sampling_hooks(
        self,
        *,
        final_answer: str,
        turn: int,
        event_callback=None,
    ):
        result = self.hook_manager.evaluate_post_sampling_hooks({
            "cwd": self.session_cwd,
            "mode": self.get_mode(),
            "agent_id": self.agent_id,
            "agent_role": self.agent_role,
            "final_answer": final_answer,
            "session_token_usage": dict(self.session_token_usage),
        })
        if self.hook_manager.has_callbacks("PostSampling"):
            observed = await self.hook_manager.run_callbacks("PostSampling", self._hook_payload(
                "PostSampling", turn=turn, final_answer=final_answer, session_token_usage=dict(self.session_token_usage)))
            for text in observed.context:
                result.outputs.append({"hook_index": len(result.outputs) + 1, "behavior": "observe",
                                       "severity": "info", "rendered": text})
        if not result.outputs:
            return

        appended_messages = []
        for item in result.outputs:
            severity = item.get("severity", "info")
            rendered = item.get("rendered", "")
            message = {
                "role": "user",
                "content": (
                    "<system-reminder>\n"
                    f"[Hook:PostSamplingHook #{item.get('hook_index')} severity={severity}]\n"
                    f"{rendered}\n"
                    "</system-reminder>"
                ),
            }
            appended_messages.append(message)
            self.post_sampling_history.insert(0, {
                "turn": turn,
                "hook_index": item.get("hook_index"),
                "severity": severity,
                "behavior": item.get("behavior", "observe"),
                "rendered": rendered,
            })

        self.post_sampling_history = self.post_sampling_history[:20]
        self.messages.extend(appended_messages)
        self._sync_message_context()
        await self._record_loop_transition(
            "post_sampling_hook",
            turn=turn,
            event_callback=event_callback,
            count=len(result.outputs),
        )
        await self._emit_stream_event(event_callback, {
            "type": "post_sampling_hook",
            "turn": turn,
            "outputs": result.outputs,
        })

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

    async def _emit_stream_event(self, event_callback, payload: dict):
        if not callable(event_callback):
            return

        result = event_callback(payload)
        if inspect.isawaitable(result):
            await result

    def _serialize_response_usage(self, response) -> dict:
        usage = getattr(response, "usage", None)
        if usage is None:
            return {}
        fields = [
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ]
        payload = {}
        for field in fields:
            value = getattr(usage, field, None)
            if value is not None:
                payload[field] = value
        return payload

    def _serialize_response_for_vcr(self, response) -> dict:
        return {
            "stop_reason": getattr(response, "stop_reason", None),
            "content": [
                block
                for block in [self._block_to_dict(item) for item in getattr(response, "content", []) or []]
                if block
            ],
            "usage": self._serialize_response_usage(response),
        }

    async def _replay_vcr_response_events(self, response, *, turn_count: int, event_callback=None):
        await self._emit_stream_event(event_callback, {
            "type": "message_start",
            "turn": turn_count,
            "replayed": True,
        })
        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", "") == "text":
                text = getattr(block, "text", "")
                if text:
                    await self._emit_stream_event(event_callback, {
                        "type": "text_delta",
                        "turn": turn_count,
                        "text": text,
                        "replayed": True,
                    })
            elif getattr(block, "type", "") == "tool_use":
                await self._emit_stream_event(event_callback, {
                    "type": "tool_scheduled",
                    "turn": turn_count,
                    "tool_name": getattr(block, "name", ""),
                    "tool_use_id": getattr(block, "id", None),
                    "replayed": True,
                })
        await self._emit_stream_event(event_callback, {
            "type": "message_stop",
            "turn": turn_count,
            "stop_reason": getattr(response, "stop_reason", None),
            "replayed": True,
        })

    def _get_model_max_output_tokens(self, model: str = "") -> tuple:
        from xuloom.core.config import get_model_max_output_tokens
        return get_model_max_output_tokens(model or self.primary_model)

    def _get_max_output_tokens(self) -> int:
        """
        Resolve max output tokens: explicit env override, delegated response cap,
        then the ordinary conversation default. Reservations still enforce all
        remaining budgets before a delegated request reaches the provider.
        """
        from xuloom.core.config import DEFAULT_CAPPED_MAX_TOKENS
        env_val = os.environ.get("XULOOM_MAX_OUTPUT_TOKENS", "")
        if env_val:
            try:
                parsed = int(env_val)
                if parsed > 0:
                    return parsed
            except ValueError:
                pass
        default_tokens, _ = self._get_model_max_output_tokens()
        from xuloom.agents.collaboration import DELEGATION_BUDGET
        delegation = DELEGATION_BUDGET.get()
        if delegation is not None:
            return min(default_tokens, delegation["request_cap"])
        return min(default_tokens, DEFAULT_CAPPED_MAX_TOKENS)

    def _build_loop_state(self) -> dict:
        env_max = os.environ.get("XULOOM_MAX_TURNS", "")
        max_turns = int(env_max) if env_max.isdigit() else 1000
        if hasattr(self, "_max_turns_override") and self._max_turns_override:
            max_turns = self._max_turns_override
        output_budget = 200000
        if hasattr(self, "_output_budget_override") and self._output_budget_override:
            output_budget = self._output_budget_override
        return {
            "max_turns": max_turns,
            "turn_count": 0,
            "max_tokens": self._get_max_output_tokens(),
            "active_model": self.primary_model,
            "fallback_count": 0,
            "session_output_budget": output_budget,
            "session_input_budget": output_budget,
            "turn_input_tokens": 0,
            "turn_output_tokens": 0,
            "max_tokens_recovery_attempts": 0,
            "max_tokens_escalations": 0,
            "reactive_compact_retries": 0,
            "stop_hook_retries": 0,
            "structured_output_retries": 0,
            "continue_reason": "initial_turn",
        }

    async def _record_loop_transition(self, transition_reason: str, *, turn: int, event_callback=None, **details):
        record = {
            "reason": transition_reason,
            "turn": turn,
            **details,
        }
        self.loop_transition_history.insert(0, record)
        self.loop_transition_history = self.loop_transition_history[:20]
        self.tool_context["loop_transition_history"] = self.loop_transition_history
        await self._emit_stream_event(event_callback, {
            "type": "loop_transition",
            **record,
        })

    def _build_continuation_prompt(self) -> str:
        return (
            "<system-reminder>\n"
            "The previous assistant response stopped because the output token budget was exhausted.\n"
            "Continue immediately from where you left off. Do not repeat completed content.\n"
            "</system-reminder>"
        )




    def _build_stop_hook_feedback(self, reason: str) -> str:
        return (
            "<system-reminder>\n"
            "Stop hook blocked the previous answer.\n"
            "Revise the answer and try again.\n"
            f"Blocking reason:\n{reason}\n"
            "</system-reminder>"
        )

    def _record_token_usage(
        self,
        response,
        loop_state: dict,
        turn: int,
        *,
        system_prompt: str = "",
        api_messages: list = None,
        tools_schema: list = None,
        thinking_config: dict = None,
        replayed: bool = False,
    ):
        usage = getattr(response, "usage", None)
        turn_record = {
            "turn": turn,
            "model": loop_state.get("active_model"),
            "input_tokens": int(getattr(usage, "input_tokens", 0) or 0) if usage is not None else 0,
            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0) if usage is not None else 0,
            "cache_creation_input_tokens": int(getattr(usage, "cache_creation_input_tokens", 0) or 0) if usage is not None else 0,
            "cache_read_input_tokens": int(getattr(usage, "cache_read_input_tokens", 0) or 0) if usage is not None else 0,
        }
        source = "api_usage"
        if turn_record["input_tokens"] <= 0 and turn_record["output_tokens"] <= 0:
            turn_record["input_tokens"] = self.compactor.estimate_request_tokens(
                api_messages or [],
                system=system_prompt,
                tools=tools_schema,
                thinking=thinking_config,
            )
            turn_record["output_tokens"] = self.compactor.estimate_content_tokens(
                "assistant",
                getattr(response, "content", []),
            )
            source = self.compactor._local_estimator_mode()
        turn_record["source"] = source

        self.token_usage_history.insert(0, turn_record)
        self.token_usage_history = self.token_usage_history[:50]
        self.tool_context["token_usage_history"] = self.token_usage_history

        for key in [
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ]:
            self.session_token_usage[key] = int(self.session_token_usage.get(key, 0) or 0) + turn_record[key]

        loop_state["turn_input_tokens"] = turn_record["input_tokens"]
        loop_state["turn_output_tokens"] = turn_record["output_tokens"]
        # Cost: replayed (VCR) responses cost nothing; estimated usage is still priced.
        cost = 0.0 if replayed else self.cost_meter.record(turn_record["model"], turn_record)
        turn_record["cost_usd"] = None if cost is None else round(cost, 8)
        if cost is None:
            self.session_cost["unpriced_requests"] = int(self.session_cost.get("unpriced_requests", 0) or 0) + 1
        else:
            self.session_cost["usd"] = float(self.session_cost.get("usd", 0.0) or 0.0) + cost
        loop_state["turn_cost_usd"] = turn_record["cost_usd"]

    def _model_span_attributes(self, response, loop_state: dict, replayed: bool) -> dict:
        record = self.token_usage_history[0] if self.token_usage_history else {}
        return {
            "gen_ai.response.model": loop_state.get("active_model"),
            "gen_ai.response.finish_reasons": [str(getattr(response, "stop_reason", "") or "")],
            "gen_ai.usage.input_tokens": record.get("input_tokens"),
            "gen_ai.usage.output_tokens": record.get("output_tokens"),
            "gen_ai.usage.cache_creation_input_tokens": record.get("cache_creation_input_tokens"),
            "gen_ai.usage.cache_read_input_tokens": record.get("cache_read_input_tokens"),
            "xuloom.usage.source": record.get("source"),
            "xuloom.cost_usd": record.get("cost_usd"),
            "xuloom.replayed": replayed,
        }

    def _remaining_output_budget(self, loop_state: dict) -> int:
        from xuloom.core.output_limits import active_budgets
        from xuloom.features.goals import available_output
        budgets = active_budgets(self.tool_context.get("goal_budget"))
        if budgets:
            return min(available_output(budget) for budget in budgets)
        remaining = max(
            0,
            int(loop_state.get("session_output_budget", 0) or 0)
            - int(self.session_token_usage.get("output_tokens", 0) or 0),
        )
        return remaining

    def _is_model_fallback_error(self, error: Exception) -> bool:
        adapter = self._model_adapter()
        if adapter is not None and not adapter.native and adapter.is_transient_error(error):
            return True
        # Inspect only SDKs already loaded by the selected connection.
        for name in ("anthropic", "openai"):
            sdk = sys.modules.get(name)
            transient = tuple(getattr(sdk, kind) for kind in ("APIConnectionError", "RateLimitError", "InternalServerError")
                if isinstance(getattr(sdk, kind, None), type))
            if isinstance(error, transient):
                return True
            status = getattr(sdk, "APIStatusError", None)
            if isinstance(status, type) and isinstance(error, status):
                return getattr(error, "status_code", None) in {500, 502, 503, 504, 529}

        rendered = str(error).lower()
        markers = [
            "overloaded",
            "service unavailable",
            "temporarily unavailable",
            "rate limit",
            "connection error",
        ]
        return any(marker in rendered for marker in markers)

    async def _stream_model_response(
        self, **kwargs,
    ):
        # A draft generated before outstanding evidence is collected is process
        # commentary, even when the provider ends that model message normally.
        workers = self.tool_context["subagent_tasks"]
        from xuloom.agents.ultra_execution import required as ultra_required
        provisional = ultra_required(self) or any(not task.done() for task in workers.tasks.values()) or bool(set(workers.results) - workers.delivered)
        callback = kwargs.get("event_callback")
        if provisional and callable(callback):
            async def commentary(event):
                if event.get("type") in {"message_start", "text_delta"}:
                    event = {**event, "channel": "commentary"}
                result = callback(event)
                if inspect.isawaitable(result):
                    await result
            kwargs["event_callback"] = commentary
        if self.tool_context.get("external_backend"):
            from xuloom.agents.external.runtime import sample
            return await sample(self, **kwargs)
        from xuloom.core.output_limits import model_reservation
        from xuloom.context.compaction import output_limit, policy
        kwargs["max_tokens"] = output_limit(self, kwargs)
        async with model_reservation(self, kwargs["max_tokens"]) as grant:
            kwargs["max_tokens"] = grant["maximum"]
            from xuloom.core.model_specs import official
            adapter = self._model_adapter()
            if adapter is not None and not adapter.native:
                response, entries, fallback = await self._receive_adapter_response(adapter, **kwargs)
            elif policy(self)["backend"] == "openai-responses" or official(self.model_provider, kwargs['model_name'], self.api_base_url):
                from xuloom.context.responses import receive
                response, entries, fallback = await receive(self, **kwargs)
            else:
                if self.native_context: raise ValueError("Native compacted history requires its declared Responses connection")
                response, entries, fallback = await self._receive_model_response(**kwargs)
            usage = self._serialize_response_usage(response)
            from xuloom.context.compaction import calibrate
            calibrate(self, kwargs, usage)
            grant["reported"] = int(self._serialize_response_usage(response).get("output_tokens", 0) or 0)
            return response, entries, fallback

    async def _receive_adapter_response(self, adapter, *, system_prompt, api_messages, tools_schema, turn_count,
                                        model_name, max_tokens, thinking_config=None, sys_print_callback=print,
                                        event_callback=None):
        """Run one turn through a registered (non-native) ModelAdapter.

        Tool calls are executed by the loop after the response, so no streaming
        tool entries are returned.
        """
        from xuloom.models.adapter import ModelRequest, STOP_REASONS
        self.tool_context["request_model"] = model_name

        streamed = []

        async def emit(event):
            if event.get("type") == "text_delta":
                streamed.append(True)
            await self._emit_stream_event(event_callback, event)

        request = ModelRequest(client=self.client, model=model_name, system_prompt=system_prompt,
                               messages=api_messages, tools=tools_schema, max_tokens=max_tokens, turn=turn_count,
                               thinking=thinking_config if self._thinking_config_explicit else None,
                               reasoning_effort=self.reasoning_effort if model_name == self.primary_model else None,
                               _emit=emit, _engine=self)
        await emit({"type": "model_status", "status": "waiting", "turn": turn_count})
        await emit({"type": "message_start", "turn": turn_count})
        task = asyncio.ensure_future(adapter.stream_turn(request))
        abort = asyncio.ensure_future(self.abort_event.wait())
        try:
            done, _ = await asyncio.wait({task, abort}, return_when=asyncio.FIRST_COMPLETED)
            if task not in done:
                raise AbortRequestedError("Abort requested while waiting for the model adapter.")
            response = task.result()
        finally:
            # Every exit path (stop request, outer cancellation, errors) ends the
            # adapter call too; an orphaned request must not keep running.
            pending = [item for item in (task, abort) if not item.done()]
            for item in pending:
                item.cancel()
            if pending:
                await _drain_cancelled(pending)
        if getattr(response, "stop_reason", None) not in STOP_REASONS or not isinstance(getattr(response, "content", None), list):
            raise TypeError(f"Model adapter {adapter.name!r} must return xuloom.models.model_response(...)")
        if not streamed:
            text = "".join(getattr(block, "text", "") for block in response.content if getattr(block, "type", "") == "text")
            if text:
                await emit({"type": "text_delta", "turn": turn_count, "text": text})
        for block in response.content:
            if getattr(block, "type", "") == "tool_use":
                await emit({"type": "tool_scheduled", "turn": turn_count, "tool_name": block.name, "tool_use_id": block.id})
        await emit({"type": "message_stop", "turn": turn_count, "stop_reason": response.stop_reason})
        return response, [], False

    # ------------------------------------------------------------------
    # Turn loop. `_run_loop` orchestrates; each phase lives in its own method
    # and returns either `_CONTINUE` (start the next iteration), a RunResult
    # (finish the run) or ``None`` (fall through to the next phase).
    # ------------------------------------------------------------------

    async def _run_loop(self, user_input: str, sys_print_callback=print, event_callback=None, input_blocks=None, attachments=None, display_text=None, selected_skills=None, resume=False):
        """Run one task inside a ``run`` span; a new top-level task restarts the cost cap."""
        if self.agent_depth == 0 and not resume:
            self.cost_meter.start_task()
        span = self.tracer.start("xuloom.run", "run", **{
            "xuloom.session_id": self.session_id, "xuloom.agent_id": self.agent_id,
            "xuloom.agent_role": self.agent_role, "xuloom.agent_depth": self.agent_depth,
            "xuloom.resume": bool(resume), "gen_ai.request.model": self.primary_model})
        try:
            with self.tracer.activate(span):
                result = await self._run_task(
                    user_input, sys_print_callback=sys_print_callback, event_callback=event_callback,
                    input_blocks=input_blocks, attachments=attachments, display_text=display_text,
                    selected_skills=selected_skills, resume=resume)
        except BaseException as error:
            self.tracer.end(span, error=error, **self._run_span_totals())
            raise
        reason = getattr(result, "reason", "")
        if reason not in {"completed", ""}:
            span.status, span.error = "error", reason
        self.tracer.end(span, **{"xuloom.result.reason": reason, **self._run_span_totals()})
        return result

    async def _stop_for_cost(self, reason, turn_count, event_callback):
        await self._record_loop_transition(
            "cost_limit", turn=turn_count, event_callback=event_callback,
            task_cost_usd=round(self.cost_meter.task_usd, 6), max_cost_usd=self.cost_meter.limit_usd)
        self.persist_session_state()
        return finish_run(self, reason, 'cost_limit')

    def _run_span_totals(self) -> dict:
        return {"xuloom.task.cost_usd": round(self.cost_meter.task_usd, 6),
                "xuloom.task.unpriced_requests": self.cost_meter.task_unpriced,
                "xuloom.session.cost_usd": round(float(self.session_cost.get("usd", 0.0) or 0.0), 6)}

    async def _run_task(self, user_input: str, sys_print_callback=print, event_callback=None, input_blocks=None, attachments=None, display_text=None, selected_skills=None, resume=False):
        """Run one task: prepare input, then iterate model request -> tools until a final answer.

        Phases: `_begin_run` (gates, skills, user message, recovery), `_drain_inbound_messages`,
        `_build_turn_request`, model sampling, `_run_tool_phase`, `_recover_max_tokens`,
        `_finish_turn` and `_handle_turn_exception`.
        """
        early, user_msg, completion = await self._begin_run(
            user_input, sys_print_callback=sys_print_callback, event_callback=event_callback,
            input_blocks=input_blocks, attachments=attachments, display_text=display_text,
            selected_skills=selected_skills, resume=resume)
        if early is not None:
            return early

        loop_state = self._build_loop_state()
        counters = {"protocol_failures": 0, "protocol_retries": 0, "input_failures": 0}

        while loop_state["turn_count"] < loop_state["max_turns"]:
            from xuloom.core.steering import consume
            try:
                await consume(self, event_callback)
            except PersistenceError as error:
                return finish_run(self, str(error), 'persistence_error')
            mailbox = await self._drain_inbound_messages()
            loop_state["turn_count"] += 1
            turn_count = loop_state["turn_count"]
            assistant_message = None
            streaming_tool_entries = []
            request = None
            loop_state["turn_input_tokens"] = 0
            loop_state["turn_output_tokens"] = 0

            # Recompute tools schema every turn so mode changes
            # (enter_plan_mode / exit_plan_mode) take effect immediately.
            tools_schema = self._get_anthropic_tools_schema()

            try:
                if self.abort_event.is_set():
                    raise AbortRequestedError("Abort requested before turn start.")
                from xuloom.core.continuation import advance
                await advance(self, turn=turn_count, event_callback=event_callback, sys_print_callback=sys_print_callback)
                # Inputs arriving during native continuation must reach the actor
                # before any new reasoning request, using the existing inbox path.
                if self.user_inbox or mailbox and mailbox():
                    continue
                blocked = self.cost_meter.blocked_reason(loop_state.get("active_model"))
                if blocked:
                    return await self._stop_for_cost(blocked, turn_count, event_callback)
                await self._apply_layered_compaction_if_needed(sys_print_callback, event_callback=event_callback)
                if self.user_inbox: continue
                remaining_output_budget = self._remaining_output_budget(loop_state)
                if remaining_output_budget <= 0:
                    await self._record_loop_transition(
                        "session_output_budget_exhausted",
                        turn=turn_count,
                        event_callback=event_callback,
                        session_output_budget=loop_state["session_output_budget"],
                        output_tokens_used=self.session_token_usage.get("output_tokens", 0),
                    )
                    self.persist_session_state()
                    return finish_run(self, ("Session output token budget exhausted before another turn could start."), 'budget_exhausted')
                blocked = self.cost_meter.blocked_reason(loop_state.get("active_model"))
                if blocked:  # compaction may have spent the rest of the budget
                    return await self._stop_for_cost(blocked, turn_count, event_callback)
                request = await self._build_turn_request(
                    loop_state, turn_count, tools_schema, remaining_output_budget, event_callback)

                model_span = self.tracer.start("xuloom.model", "model", **{
                    "gen_ai.operation.name": "chat", "gen_ai.system": self.model_provider,
                    "gen_ai.request.model": loop_state.get("active_model"),
                    "gen_ai.request.max_tokens": request["max_tokens"], "xuloom.turn": turn_count})
                try:
                    streaming_fallback = False
                    replayed_response = self.vcr_manager.try_replay(request["vcr_payload"])
                    if replayed_response is not None:
                        response = replayed_response
                        streaming_tool_entries = []
                        await self._replay_vcr_response_events(
                            response,
                            turn_count=turn_count,
                            event_callback=event_callback,
                        )
                    else:
                        response, streaming_tool_entries, streaming_fallback = await self._stream_model_response(
                            system_prompt=request["system_prompt"],
                            api_messages=request["api_messages"],
                            tools_schema=tools_schema,
                            turn_count=turn_count,
                            model_name=loop_state["active_model"],
                            max_tokens=request["max_tokens"],
                            thinking_config=request["thinking_config"],
                            sys_print_callback=sys_print_callback,
                            event_callback=event_callback,
                        )
                        if streaming_fallback:
                            await self._emit_stream_event(event_callback, {
                                "type": "streaming_fallback",
                                "turn": turn_count,
                            })
                        self.vcr_manager.record(
                            request["vcr_payload"],
                            self._serialize_response_for_vcr(response),
                        )
                        if self.response_recorder is not None:
                            self.response_recorder.append(turn=turn_count, model=loop_state.get("active_model") or "",
                                                          response=self._serialize_response_for_vcr(response),
                                                          session_id=self.session_id)
                    self._record_token_usage(
                        response,
                        loop_state,
                        turn_count,
                        system_prompt=request["system_prompt"],
                        api_messages=request["api_messages"],
                        tools_schema=tools_schema,
                        thinking_config=request["thinking_config"],
                        replayed=replayed_response is not None,
                    )
                except BaseException as error:
                    self.tracer.end(model_span, error=error)
                    raise
                self.tracer.end(model_span, **self._model_span_attributes(response, loop_state, replayed_response is not None))
                counters["protocol_failures"] = 0
                await self._emit_stream_event(event_callback, {
                    "type": "token_usage",
                    "turn": turn_count,
                    "input_tokens": loop_state["turn_input_tokens"],
                    "output_tokens": loop_state["turn_output_tokens"],
                    "session_input_tokens": self.session_token_usage.get("input_tokens", 0),
                    "session_output_tokens": self.session_token_usage.get("output_tokens", 0),
                    "cost_usd": loop_state.get("turn_cost_usd"),
                    "session_cost_usd": round(float(self.session_cost.get("usd", 0.0) or 0.0), 6),
                    "task_cost_usd": round(self.cost_meter.task_usd, 6),
                })
                await self._emit_stream_event(event_callback, {
                    "type": "thinking_config",
                    "turn": turn_count,
                    "config": request["thinking_config"],
                    "mode": self.get_mode(),
                })

                content_as_dicts = [self._block_to_dict(b) for b in response.content]
                from xuloom.agents.ultra_execution import required as ultra_required
                if ultra_required(self) and response.stop_reason != 'tool_use':
                    raise ToolProtocolError('Ultra requires ultra_answer for a simple task or ultra_prepare for substantive work before a final response.',
                        code='ultra_assessment_missing', raw_text='\n'.join(b.get('text','') for b in content_as_dicts if b.get('type')=='text'))
                content_as_dicts = [b for b in content_as_dicts if b is not None]
                assistant_message = create_assistant_message(
                    content_as_dicts,
                    model=loop_state.get("active_model", ""),
                    stop_reason=getattr(response, "stop_reason", ""),
                )
                if getattr(response, "responses_output", None):
                    assistant_message["_responses_output"] = response.responses_output
                self.messages.append(assistant_message)
                self._sync_message_context()
                # Checkpointing memory auto-flush
                self.persist_session_state()
                if self.user_inbox and response.stop_reason != "tool_use": continue

                if response.stop_reason == "tool_use":
                    outcome = await self._run_tool_phase(
                        response, content_as_dicts, assistant_message, streaming_tool_entries,
                        loop_state, counters, turn_count, sys_print_callback, event_callback)
                    if isinstance(outcome, RunResult):
                        return outcome
                    content_as_dicts, assistant_message = outcome

                if response.stop_reason == "max_tokens":
                    outcome = await self._recover_max_tokens(
                        streaming_tool_entries, loop_state, turn_count, sys_print_callback, event_callback)
                elif response.stop_reason != "tool_use":
                    outcome = await self._finish_turn(
                        content_as_dicts, assistant_message, streaming_tool_entries, completion,
                        loop_state, turn_count, sys_print_callback, event_callback)
                else:
                    outcome = None
                if outcome is _CONTINUE:
                    continue
                if outcome is not None:
                    return outcome

            except Exception as e:
                if streaming_tool_entries:
                    await self._cancel_streaming_tool_tasks(streaming_tool_entries)
                outcome = await self._handle_turn_exception(
                    e, assistant_message, request, tools_schema, loop_state, counters,
                    turn_count, sys_print_callback, event_callback)
                if outcome is _CONTINUE:
                    continue
                return outcome

        return finish_run(self, ("[bold red]System Halt:[/bold red] "
            f"Max internal reasoning turns ({loop_state['max_turns']}) reached without returning an answer."), 'turn_limit')

    async def _begin_run(self, user_input, *, sys_print_callback, event_callback, input_blocks, attachments,
                         display_text, selected_skills, resume):
        """Gate the run, expand selected skills, append (or resume) the user task.

        Returns ``(early_result, user_message, workflow_completion)``; a non-None
        ``early_result`` ends the run before any model request.
        """
        if self.tool_context.get("delegation", {}).get("authority_required") and self.permission_manager.delegated_authorizer is None:
            return RunResult("This is an owned delegated execution. Resume it through its original controller, or fork an independent conversation.",
                "delegation_unavailable", session_id=self.session_id, agent_id=self.agent_id), None, None
        try:
            self.require_writer()
        except (SessionBusyError, SessionConflictError) as error:
            return RunResult(str(error), "session_busy" if isinstance(error, SessionBusyError) else "session_conflict", session_id=self.session_id, agent_id=self.agent_id), None, None
        if resume and (user_input or input_blocks or attachments or selected_skills):
            raise ValueError("Resume cannot replace the original prompt, attachments or skills")
        from xuloom.core.model_inputs import validate_media, validate_transport, UnsupportedInput
        try:
            input_history = self.messages + ([{"content": input_blocks}] if input_blocks else [])
            validate_transport(input_history, self.model_provider)
            validate_media(input_history,
                self._input_contract(self.primary_model))
        except UnsupportedInput as error:
            return finish_run(self, str(error), 'unsupported_input'), None, None
        if self.agent_depth == 0 and not resume:
            from xuloom.agents.collaboration import new_run, policy
            profile = policy(self.collaboration_mode, self.tool_context["skill_registry"].settings())
            self.tool_context["collaboration"] = profile
            self.tool_context["collaboration_run"] = new_run(profile)
        self.abort_event.clear()
        completion = self.tool_context["workflow_completion"]
        if not resume:
            completion.reset()
        user_input, input_blocks, display_text = await self._apply_selected_skills(
            user_input, input_blocks, display_text, selected_skills, completion, event_callback)

        if not self.is_configured:
            return finish_run(self, ("Model not configured. Use /model to set protocol, model name, "
                "base URL, and API key before sending messages."), 'not_configured'), None, None

        if resume:
            user_msg = next((message for message in reversed(self.messages) if message.get("role") == "user"
                and message.get("_origin", "human") == "human" and not message.get("_is_meta")
                and not message.get("_is_tool_result") and not any(block.get("type") == "tool_result"
                    for block in message.get("content", []) if isinstance(block, dict))), None)
            if user_msg is None:
                raise ValueError("There is no original user task to resume")
            # Use the existing recovery receipts, never replay uncertain tools.
            restore_tool_activity(self, {"tool_activity": {"in_progress": self.in_progress_tools,
                "recent": self.recent_tool_activity}})
            self.messages.append(create_user_message(
                "[Task resumed by user]\nContinue the original interrupted task using its existing instructions, "
                "attachments, partial response and saved tool results. Continue the unfinished response without "
                "repeating its existing text or adding a restart announcement. Do not repeat completed operations. "
                "Interrupted tools may have uncertain effects: inspect saved results/current state before any new action. "
                "Cancelled questions or approvals are not answers or authorization; ask again if still needed.",
                is_meta=True, is_continuation=True, origin="resume_control"))
        else:
            prompt_hook = await self._dispatch_hook("UserPromptSubmit", self._hook_payload(
                "UserPromptSubmit", prompt=user_input if display_text is None else display_text,
                attachments=len(attachments or []), selected_skills=list(selected_skills or [])), queue_context=False)
            if prompt_hook.decision == "block":
                return finish_run(self, "已被 UserPromptSubmit Hook 拦截：" + (prompt_hook.reason or "未说明原因"),
                                  'hook_blocked'), None, None
            user_msg = create_user_message(input_blocks if input_blocks is not None else user_input, origin="human")
            if attachments:
                user_msg["_attachments"] = attachments
            if display_text is not None:
                user_msg["_display_text"] = display_text
            self.messages.append(user_msg)
            self._append_hook_messages("UserPromptSubmit", prompt_hook.context)
        if self.active_turn_trace is not None:
            self.active_turn_trace["userMessageId"] = get_msg_uuid(user_msg)
        self._sync_message_context()

        if not hasattr(self, "session_started"):
            self.session_started = False
        if not self.session_started:
            session_start = await self._dispatch_hook("SessionStart", self._hook_payload("SessionStart"), queue_context=False)
            self._append_hook_messages("SessionStart", session_start.context)
            self.session_started = True
            self.persist_session_state()

        if resume:
            from xuloom.agents.ultra_decision import recover
            try:
                await recover(self, user_msg, event_callback=event_callback, sys_print_callback=sys_print_callback)
            except (ValueError, PersistenceError) as error:
                return finish_run(self, str(error), 'delegation_recovery_error'), None, None
            except AbortRequestedError:
                return finish_run(self, "Ultra recovery cancelled; existing receipts were retained.", 'cancelled'), None, None
        return None, user_msg, completion

    async def _apply_selected_skills(self, user_input, input_blocks, display_text, selected_skills, completion, event_callback):
        """Expand `$skill` mentions and explicit selections into the user turn."""
        registry = self.tool_context["skill_registry"]
        names = list(dict.fromkeys(selected_skills or []))
        # CLI mentions and desktop selections use the same loader and stored input.
        for mention in re.finditer(r"(?:^|\s)\$([a-z][a-z0-9]*(?:-[a-z0-9]+)*)(?=\s|$)", user_input):
            name = mention.group(1)
            if name not in names:
                names.append(name)
        selections, seen = [], set()
        for name in names:
            skill = registry.resolve(name)
            if skill["id"] not in seen:
                selections.append((skill, registry.read(skill["name"])))
                seen.add(skill["id"])
        if selections:
            display_text = user_input if display_text is None else display_text
            instructions = "\n\n<selected-skills>\n" + "\n\n".join(body for _, body in selections) + "\n</selected-skills>"
            if input_blocks is None:
                user_input += instructions
            else:
                input_blocks = copy.deepcopy(input_blocks) + [{"type": "text", "text": instructions}]
            for skill, _ in selections:
                completion.activate(skill)
                loaded = self.tool_context.setdefault("loaded_skills", [])
                if skill["name"] not in loaded:
                    loaded.append(skill["name"])
                await self._emit_stream_event(event_callback, {"type": "skill_loaded", "name": skill["name"]})
        return user_input, input_blocks, display_text

    async def _drain_inbound_messages(self):
        """Move parent, team and background-worker messages into the transcript.

        Returns the delegated mailbox callable (or None) so callers can re-check it.
        """
        mailbox = self.tool_context.get("delegated_mailbox")
        if mailbox:
            incoming = mailbox()
            if incoming:
                seen = {message.get("_delegated_message_id") for message in self.messages}
                for message in incoming:
                    if message["id"] not in seen:
                        item = create_user_message("Parent message: " + message["text"], origin="subagent")
                        item["_delegated_message_id"] = message["id"]
                        self.messages.append(item)
                        seen.add(message["id"])
                self._sync_message_context()
                self.persist_session_state()
                if self.session_manager.last_save_ok:
                    self.tool_context["ack_delegated_mailbox"]({m["id"] for m in incoming})
        if self.team_manager.is_active:
            while True:
                team_message = await self.team_manager.receive_message(self.agent_id)
                if team_message is None:
                    break
                self.messages.append(create_user_message("Team message from {}: {}".format(team_message.from_name, team_message.text), origin="team"))
        for report in self.tool_context["subagent_tasks"].completed():
            self.messages.append(create_user_message("Background subagent completed: " + str(report), origin="subagent"))
        return mailbox

    async def _build_turn_request(self, loop_state, turn_count, tools_schema, remaining_output_budget, event_callback):
        """Assemble system prompt, API messages and thinking config for one model request."""
        self._flush_hook_context()
        self._inject_turn_attachments(turn_count)
        # Archive oversized results before request assembly.
        from xuloom.agents import ultra_decision
        boundary_messages = ultra_decision.request_messages(self, self._get_messages_after_compact_boundary())
        self._apply_tool_result_budget(boundary_messages)
        api_messages = self._clean_roles_for_api(boundary_messages)
        if self.model_provider == "anthropic" and self._is_native_anthropic():
            api_messages = self._apply_message_cache_control(api_messages)
        loaded_memory_files = self.refresh_memory_files()
        effective_thinking_config = self._resolve_thinking_config()
        system_prompt = self.context_builder.generate_system_prompt(
            session_summary=self.plan_manager.render_prompt_summary(),
            todo_summary=self.todo_manager.render_prompt_summary(),
            memory_summary=self.get_memory_summary(),
            structured_output_summary=self.structured_output_manager.render_prompt_summary(),
            tool_prompt_summary=self._build_tool_prompt_summary(),
            collaboration_summary=self._collaboration_instructions(),
            mcp_instructions=self._build_mcp_instructions(),
        )
        system_prompt += self._get_coordinator_system_prompt_addon()
        await self._emit_stream_event(event_callback, {
            "type": "memory_files",
            "turn": turn_count,
            "count": len(loaded_memory_files),
            "files": [
                {
                    "scope": item.get("scope"),
                    "path": item.get("path"),
                    "truncated": item.get("truncated", False),
                }
                for item in loaded_memory_files
            ],
        })
        max_tokens = min(loop_state["max_tokens"], remaining_output_budget)
        vcr_payload = self.vcr_manager.build_request_payload(
            provider=self.model_provider,
            model=loop_state["active_model"],
            system=system_prompt,
            messages=api_messages,
            tools=tools_schema,
            max_tokens=max_tokens,
            thinking=effective_thinking_config,
            reasoning={"profile": self.reasoning_profile if loop_state["active_model"] == self.primary_model else configured_profile(self.model_provider, loop_state["active_model"], self.api_base_url),
                "effort": self.reasoning_effort if loop_state["active_model"] == self.primary_model else None},
        )
        return {"system_prompt": system_prompt, "api_messages": api_messages, "max_tokens": max_tokens,
                "thinking_config": effective_thinking_config, "vcr_payload": vcr_payload}

    async def _run_tool_phase(self, response, content_as_dicts, assistant_message, streaming_tool_entries,
                              loop_state, counters, turn_count, sys_print_callback, event_callback):
        """Execute requested tools and append their results.

        Returns a RunResult to stop, or ``(content_as_dicts, assistant_message)``
        (possibly replaced by an Ultra direct answer that ends the turn).
        """
        from xuloom.agents import ultra_decision
        loop_state["workflow_guard_stalls"] = 0
        if streaming_tool_entries:
            tool_results = []
            for item in streaming_tool_entries:
                if self.abort_event.is_set():
                    raise AbortRequestedError("Abort requested while awaiting tool results.")
                tool_results.append(await item["task"])
        else:
            tool_calls = [b for b in content_as_dicts if isinstance(b, dict) and b.get("type") == "tool_use"]
            tool_results = []
            results = await self._execute_tool_batches(
                tool_calls,
                sys_print_callback,
                turn=turn_count,
                event_callback=event_callback,
            )
            tool_results.extend(results)

        tool_result_msg = create_tool_result_message(
            tool_results,
            source_assistant_uuid=get_msg_uuid(assistant_message),
        )
        self.messages.append(tool_result_msg)
        ultra_decision.receipts_saved(self, tool_results)
        self._flush_hook_context()
        self._sync_message_context()
        if any(item.get("_input_error") for item in tool_results):
            counters["input_failures"] += 1
            input_failures = counters["input_failures"]
            retrying = input_failures <= 2 and counters["protocol_retries"] < 6
            message = (f"工具参数校验失败，正在请求模型修正（{input_failures}/2）。已完成的操作保留。" if retrying else
                       "工具参数多次修正失败，任务已中断。已完成的操作和文件保留，可继续当前会话。")
            await self._record_loop_transition("tool_input_repair" if retrying else "tool_input_exhausted",
                turn=turn_count, event_callback=event_callback, attempt=input_failures, retrying=retrying)
            await self._emit_stream_event(event_callback, {"type": "tool_protocol_error", "message": message,
                "retrying": retrying, "attempt": input_failures, "limit": 2,
                "detail": "Invalid tool input; see the rejected call's tool result."})
            sys_print_callback(message)
            self.persist_session_state()
            if not retrying:
                halted = create_assistant_message(message, model=loop_state["active_model"], stop_reason="tool_input_error")
                halted["_is_meta"] = True
                self.messages.append(halted)
                self._sync_message_context()
                self.persist_session_state()
                return finish_run(self, ("[bold red]API Loop Exception:[/bold red] " + message), 'tool_input_error')
            counters["protocol_retries"] += 1
        else:
            counters["input_failures"] = 0

        assessment = self.tool_context.get('collaboration_run', {}).get('assessment', {})
        if assessment.get('kind') == 'direct' and any(b.get('name')=='ultra_answer' for b in content_as_dicts) and not any(item.get('is_error') for item in tool_results):
            # The simple answer was produced in the assessment request;
            # reuse the normal finalization/StopHook path without another model call.
            content_as_dicts = [{'type':'text','text':assessment['answer']}]
            response.stop_reason = 'end_turn'
            assistant_message = create_assistant_message(content_as_dicts, model=loop_state['active_model'], stop_reason='end_turn')
            self.messages.append(assistant_message)
            self._sync_message_context()
            await self._emit_stream_event(event_callback, {'type':'message_start','turn':turn_count})
            await self._emit_stream_event(event_callback, {'type':'text_delta','turn':turn_count,'text':assessment['answer']})
            await self._emit_stream_event(event_callback, {'type':'message_stop','turn':turn_count,'stop_reason':'end_turn'})

        # Diagnostics exist only after a language-server tool was explicitly used.
        if hasattr(self, 'lsp_manager'):
            diags_msg = self.lsp_manager.consume_diagnostics()
            if diags_msg:
                self.messages.append(create_user_message(
                    f"[System Observation]\n{diags_msg}",
                    is_meta=True,
                    origin="lsp_diagnostics",
                ))
                self._sync_message_context()
                sys_print_callback("[bold red]⚠ Active Compiler Diagnostics intercepted and fed to agent loop![/bold red]")

        # Tool cycle complete, checkpoint again
        self.persist_session_state()
        loop_state["continue_reason"] = "next_turn"
        await self._record_loop_transition(
            "next_turn",
            turn=turn_count,
            event_callback=event_callback,
            stop_reason="tool_use",
        )
        return content_as_dicts, assistant_message

    async def _recover_max_tokens(self, streaming_tool_entries, loop_state, turn_count, sys_print_callback, event_callback):
        """Continue after a max_tokens stop, escalating the output limit once."""
        if streaming_tool_entries:
            await self._cancel_streaming_tool_tasks(streaming_tool_entries)
        loop_state["max_tokens_recovery_attempts"] += 1
        remaining_output_budget = self._remaining_output_budget(loop_state)
        if remaining_output_budget <= 0:
            await self._record_loop_transition(
                "session_output_budget_exhausted",
                turn=turn_count,
                event_callback=event_callback,
                session_output_budget=loop_state["session_output_budget"],
                output_tokens_used=self.session_token_usage.get("output_tokens", 0),
            )
            self.persist_session_state()
            return finish_run(self, ("Session output token budget exhausted before continuation."), 'budget_exhausted')
        transition_reason = "max_output_tokens_recovery"
        if (
            loop_state["max_tokens_recovery_attempts"] >= 2
            and loop_state["max_tokens_escalations"] < 1
        ):
            loop_state["max_tokens"] = 64000
            loop_state["max_tokens_escalations"] += 1
            transition_reason = "max_output_tokens_escalate"
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

    async def _finish_turn(self, content_as_dicts, assistant_message, streaming_tool_entries, completion,
                           loop_state, turn_count, sys_print_callback, event_callback):
        """Apply completion guards (workers, workflow, plan mode, structured output, StopHook) and finish."""
        if streaming_tool_entries:
            await self._cancel_streaming_tool_tasks(streaming_tool_entries)
        # Normal completion without Tool calls => Return final answer
        texts = [b.get("text", "") for b in content_as_dicts if isinstance(b, dict) and b.get("type") == "text"]
        final_answer = "\n".join(texts)
        if await self._collect_subagents_before_final(turn=turn_count, event_callback=event_callback):
            assistant_message["_stop_reason"] = "awaiting_subagents"
            self.persist_session_state()
            return _CONTINUE

        if completion.pending:
            return await self._guard_workflow_completion(completion, assistant_message, loop_state, turn_count, event_callback)

        # Plan mode guard: model ended with text-only output while plan mode
        # is active. It should have called exit_plan_mode or ask_user_question_tool.
        if (
            self.get_mode() == "plan"
            and loop_state.get("plan_mode_guard_retries", 0) < 2
        ):
            loop_state["plan_mode_guard_retries"] = loop_state.get("plan_mode_guard_retries", 0) + 1
            pfp = self.plan_manager.plan_file_path
            guard_msg = (
                "<system-reminder>\n"
                "You are in PLAN MODE. You must NOT answer the user's question directly.\n\n"
                "Your turn MUST end with one of these tool calls:\n"
                "  1. exit_plan_mode — when your plan is ready for approval\n"
                "  2. ask_user_question_tool — to clarify requirements\n\n"
                "Do NOT output a plain-text answer. Instead:\n"
                f"  - Inspect relevant materials with read-only tools\n"
                f"  - Write your plan to the plan file at {pfp}\n"
                "  - Then call exit_plan_mode\n"
                "</system-reminder>"
            )
            self.messages.append(create_user_message(
                guard_msg, is_meta=True, origin="plan_mode_guard",
            ))
            self._sync_message_context()
            self.persist_session_state()
            await self._record_loop_transition(
                "plan_mode_guard",
                turn=turn_count,
                event_callback=event_callback,
                retries=loop_state["plan_mode_guard_retries"],
            )
            sys_print_callback(
                "[dim yellow]↳ Plan mode guard: model ended without calling exit_plan_mode; injecting reminder.[/dim yellow]"
            )
            return _CONTINUE

        structured_ok, structured_reason = self.structured_output_manager.validate(final_answer)
        if not structured_ok:
            loop_state["structured_output_retries"] += 1
            self.messages.append(create_user_message(
                self._build_structured_output_feedback(structured_reason),
                is_meta=True,
                origin="structured_output_retry",
            ))
            self._sync_message_context()
            self.persist_session_state()
            await self._record_loop_transition(
                "structured_output_retry",
                turn=turn_count,
                event_callback=event_callback,
                retries=loop_state["structured_output_retries"],
                reason=structured_reason,
            )
            sys_print_callback(
                "[dim yellow]↳ Structured output validation failed; retrying with feedback.[/dim yellow]"
            )
            await self._emit_stream_event(event_callback, {"type": "model_retry",
                "message": "输出格式校验未通过，正在请求模型修正。", "attempt": loop_state["structured_output_retries"]})
            return _CONTINUE
        hook_payload = {
            "cwd": self.session_cwd,
            "mode": self.get_mode(),
            "agent_id": self.agent_id,
            "agent_role": self.agent_role,
            "final_answer": final_answer,
            "turn_input_tokens": loop_state["turn_input_tokens"],
            "turn_output_tokens": loop_state["turn_output_tokens"],
            "session_token_usage": dict(self.session_token_usage),
        }
        stop_hook_decision = await self.hook_manager.evaluate_stop_hooks_async(dict(hook_payload))
        if stop_hook_decision.behavior == "allow" and self.hook_manager.has_callbacks("Stop"):
            from xuloom.core.hooks import StopHookDecision
            stop_callbacks = await self.hook_manager.run_callbacks("Stop", self._hook_payload("Stop", **hook_payload))
            self._queue_hook_context("Stop", stop_callbacks)
            if stop_callbacks.decision in {"block", "retry"}:
                stop_hook_decision = StopHookDecision(stop_callbacks.decision, stop_callbacks.reason)
        if stop_hook_decision.behavior in {"block", "retry"}:
            loop_state["stop_hook_retries"] += 1
            self.messages.append(create_user_message(
                self._build_stop_hook_feedback(
                    stop_hook_decision.reason or "Blocked by stop hook."
                ),
                is_meta=True,
                origin="stop_hook",
            ))
            self._sync_message_context()
            self.persist_session_state()
            await self._record_loop_transition(
                "stop_hook_blocking",
                turn=turn_count,
                event_callback=event_callback,
                retries=loop_state["stop_hook_retries"],
                behavior=stop_hook_decision.behavior,
            )
            sys_print_callback(
                "[dim yellow]↳ StopHook blocked the current answer; retrying with feedback.[/dim yellow]"
            )
            return _CONTINUE
        turn_end = await self._dispatch_hook("TurnEnd", self._hook_payload("TurnEnd", **hook_payload), queue_context=False)
        self._append_hook_messages("TurnEnd", turn_end.context)
        await self._apply_post_sampling_hooks(
            final_answer=final_answer,
            turn=turn_count,
            event_callback=event_callback,
        )
        if self.abort_event.is_set():
            raise AbortRequestedError("Abort requested before turn finalization.")
        # Delegated runs own their transcript, not project-wide
        # durable memories or untracked post-completion model jobs.
        if not self.tool_context.get("delegation"):
            estimated_tokens = self.compactor.estimate_tokens(self.messages)
            await self.session_memory_manager.maybe_extract(
                self.messages, estimated_tokens,
            )
            self.memory_extractor.record_turn()
            await self.memory_extractor.maybe_extract(self.messages)
        self.persist_session_state()
        mailbox = self.tool_context.get("delegated_mailbox")
        if self.user_inbox or mailbox and mailbox():
            # A message accepted while the model was streaming is
            # still part of this assignment, not a completed reply.
            return _CONTINUE
        return finish_run(self, (final_answer), 'completed')

    async def _guard_workflow_completion(self, completion, assistant_message, loop_state, turn_count, event_callback):
        """Ask the model to continue an unfinished skill workflow, or halt after repeated early stops."""
        if self.abort_event.is_set():
            raise AbortRequestedError("Abort requested before workflow completion check.")
        stalls = loop_state.get("workflow_guard_stalls", 0) + 1
        retries = loop_state.get("workflow_guard_retries", 0)
        retrying = stalls <= 2 and retries < 6
        loop_state["workflow_guard_stalls"] = stalls
        assistant_message["_stop_reason"] = "workflow_incomplete"
        message = (f"技能流程尚未完成，正在请求模型继续执行（{stalls}/2）。" if retrying else
                   "技能流程尚未完成，模型多次提前结束，任务已中断。已有结果保留，可继续当前会话。")
        await self._emit_stream_event(event_callback, {"type": "workflow_incomplete", "message": message,
            "retrying": retrying, "attempt": stalls, "limit": 2})
        await self._record_loop_transition("workflow_completion_guard" if retrying else "workflow_completion_exhausted",
            turn=turn_count, event_callback=event_callback, retrying=retrying, attempt=stalls)
        if retrying:
            loop_state["workflow_guard_retries"] = retries + 1
            self.messages.append(create_user_message(completion.feedback(), is_meta=True, origin="workflow_completion"))
        else:
            halted = create_assistant_message(message, model=loop_state["active_model"], stop_reason="workflow_incomplete")
            halted["_is_meta"] = True
            self.messages.append(halted)
        self._sync_message_context()
        self.persist_session_state()
        if retrying:
            return _CONTINUE
        return finish_run(self, ("[bold red]System Halt:[/bold red] " + message), 'workflow_incomplete')

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
