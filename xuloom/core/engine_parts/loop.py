"""The model/tool turn loop, loop state, budgets, structured output and stop hooks."""
import asyncio
import copy
import inspect
import os
import re
from typing import Any, List, Optional
from xuloom.core.messages import create_assistant_message, create_tool_result_message, create_user_message, get_msg_uuid
from xuloom.core.model_stream import AbortRequestedError
from xuloom.core.reasoning import configured_profile
from xuloom.core.run_result import PersistenceError, RunResult, finish_run
from xuloom.core.session_lock import SessionBusyError, SessionConflictError
from xuloom.core.tool_execution import restore_tool_activity
from xuloom.core.engine_state import EngineState

NOT_CONFIGURED_MESSAGE = (
    "Model not configured. Python: pass EngineConfig(model=..., model_provider=\"openai\" or \"anthropic\", "
    "api_key=..., api_base_url=...). CLI (xuloom exec/serve/eval): set OPENAI_API_KEY and OPENAI_MODEL "
    "(or ANTHROPIC_API_KEY and ANTHROPIC_MODEL), optionally OPENAI_BASE_URL; interactive xuloom: /model. "
    "Desktop app: Settings -> Model connections.")

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


class TurnLoopMixin(EngineState):
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


    async def _emit_stream_event(self, event_callback, payload: dict):
        if not callable(event_callback):
            return

        result = event_callback(payload)
        if inspect.isawaitable(result):
            await result


    def _build_loop_state(self) -> dict:
        env_max = os.environ.get("XULOOM_MAX_TURNS", "")
        max_turns = int(env_max) if env_max.isdigit() else 1000
        if hasattr(self, "_max_turns_override") and self._max_turns_override:
            max_turns = self._max_turns_override
        output_budget = getattr(self, "_output_budget_override", None) or None
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


    def _build_stop_hook_feedback(self, reason: str) -> str:
        return (
            "<system-reminder>\n"
            "Stop hook blocked the previous answer.\n"
            "Revise the answer and try again.\n"
            f"Blocking reason:\n{reason}\n"
            "</system-reminder>"
        )


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
            streaming_tool_entries: List[Any] = []
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
                    "usage_source": (self.token_usage_history[0].get("source") if self.token_usage_history else None),
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
                content_as_dicts = [b for b in content_as_dicts if b is not None]
                assistant_message = create_assistant_message(
                    content_as_dicts,
                    model=loop_state.get("active_model", ""),
                    stop_reason=getattr(response, "stop_reason", ""),
                )
                if getattr(response, "responses_output", None):
                    assistant_message["_responses_output"] = response.responses_output
                if getattr(response, "_openai_reasoning", None) is not None:
                    assistant_message["_openai_reasoning"] = copy.deepcopy(response._openai_reasoning)
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
            return finish_run(self, NOT_CONFIGURED_MESSAGE, 'not_configured'), None, None

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
        self.tool_context["request_model"] = loop_state["active_model"]
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
        max_tokens = loop_state["max_tokens"] if remaining_output_budget is None else min(loop_state["max_tokens"], remaining_output_budget)
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
        if not any(item.get("_input_error") for item in tool_results):
            from xuloom.agents.progress import observe
            stalled = await observe(self, tool_results, loop_state, turn=turn_count, event_callback=event_callback)
            if stalled is not None:
                return stalled
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
            if getattr(response, '_openai_reasoning', None) is not None:
                assistant_message['_openai_reasoning'] = copy.deepcopy(response._openai_reasoning)
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
        pending = mailbox() if mailbox else []
        settled = {m["id"] for m in pending if m.get("kind") == "settle"}
        if settled:
            # This already-validated final result satisfies a settlement nudge.
            # A real parent/user instruction still requires another turn.
            self.tool_context["ack_delegated_mailbox"](settled)
            pending = [m for m in pending if m["id"] not in settled]
        if self.user_inbox or pending:
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
