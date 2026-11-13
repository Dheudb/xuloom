"""Provider stream decoding over QueryEngine state; no separate lifecycle or scheduler."""
from typing import Any, Dict, List, Optional
import asyncio
import inspect
import os
from types import SimpleNamespace
from xuloom.core.reasoning import configured_profile, request_options
from xuloom.protocols.dsml import ToolTextFilter, ToolProtocolError, decode_tool_text, parse_tool_arguments
from xuloom.protocols.stream_watch import StreamWatch, StreamAborted, StreamInterrupted
from xuloom.core.engine_state import EngineState


class AbortRequestedError(Exception):
    pass


class ModelStream(EngineState):
    async def _receive_model_response(
        self,
        *,
        system_prompt: str,
        api_messages: list,
        tools_schema: list,
        turn_count: int,
        model_name: str,
        max_tokens: int,
        thinking_config: Optional[dict] = None,
        sys_print_callback=print,
        event_callback=None,
    ):
        """Returns (response, tool_entries, streaming_fallback_occurred)."""
        from xuloom.core.model_inputs import validate_media, validate_transport
        validate_media(api_messages, self._input_contract(model_name))
        validate_transport(api_messages, self.model_provider)
        self.tool_context["request_model"] = model_name
        tool_entries: List[Any] = []
        self.active_streaming_tool_entries = tool_entries
        previous_batch_gate = None
        current_safe_batch_gate = None
        current_safe_batch_tasks: Optional[List[asyncio.Task]] = None
        streaming_semaphore = asyncio.Semaphore(self.MAX_TOOL_CONCURRENCY)
        streaming_sibling_cancel = asyncio.Event()
        openai_watch = None

        try:
            await self._emit_stream_event(event_callback, {"type": "model_status", "status": "waiting", "turn": turn_count})
            if self.model_provider == "openai":
                openai_messages = self._clean_roles_for_openai_api(
                    api_messages,
                    system_prompt,
                )
                create_kwargs = {
                    "model": model_name,
                    "messages": openai_messages,
                    "max_tokens": max_tokens,
                    "stream": True,
                    "stream_options": {"include_usage": True},
                }
                if tools_schema:
                    create_kwargs["tools"] = tools_schema
                from xuloom.agents.ultra_execution import required
                # Thinking-capable Chat endpoints can reject forced choice. The loop
                # still validates Ultra assessment before accepting any final answer.
                if required(self): create_kwargs.update(tool_choice='auto', parallel_tool_calls=False)
                profile = self.reasoning_profile if model_name == self.primary_model else configured_profile(self.model_provider, model_name, self.api_base_url)
                if profile is not None:
                    create_kwargs.update(request_options(profile, self.model_provider,
                        effort_override=self.reasoning_effort if model_name == self.primary_model else None, max_tokens=max_tokens))
                elif self.reasoning_effort and model_name == self.primary_model:
                    create_kwargs["reasoning_effort"] = self.reasoning_effort

                text_fragments = []
                reasoning_fragments = []
                text_filter = ToolTextFilter()
                tool_calls: Dict[int, Any] = {}
                dispatched_indices = set()
                emitted_tool_ids = set()
                finish_reason = "end_turn"
                usage_payload = self._make_usage_block()

                openai_sibling_cancel = asyncio.Event()

                def _dispatch_tool_if_ready(idx):
                    """Dispatch a tool call as soon as its arguments are fully received."""
                    if idx in dispatched_indices:
                        return
                    record = tool_calls.get(idx)
                    if not record or not record["id"] or not record["name"]:
                        return
                    dispatched_indices.add(idx)

                    tool_id = record["id"]
                    raw_args = "".join(record.get("arguments", []) or [])
                    argument_error = ""
                    try:
                        parsed_args = parse_tool_arguments(raw_args)
                    except ToolProtocolError as exc:
                        parsed_args, argument_error = {}, str(exc)

                    tool_block = SimpleNamespace(
                        type="tool_use",
                        id=tool_id,
                        name=record["name"],
                        input=parsed_args,
                        _argument_error=argument_error,
                        _raw_arguments=raw_args,
                    )

                    is_safe = self._is_concurrency_safe(record["name"], parsed_args)
                    nonlocal previous_batch_gate, current_safe_batch_tasks, current_safe_batch_gate

                    if is_safe:
                        if current_safe_batch_tasks is None:
                            current_safe_batch_gate = previous_batch_gate
                            current_safe_batch_tasks = []
                        task = asyncio.create_task(
                            self._await_gate_and_execute_tool(
                                current_safe_batch_gate,
                                tool_block,
                                sys_print_callback,
                                turn=turn_count,
                                event_callback=event_callback,
                                semaphore=streaming_semaphore,
                                sibling_cancel=openai_sibling_cancel,
                            )
                        )
                        current_safe_batch_tasks.append(task)
                    else:
                        if current_safe_batch_tasks is not None:
                            previous_batch_gate = asyncio.gather(*current_safe_batch_tasks)
                            current_safe_batch_tasks = None
                            current_safe_batch_gate = None
                        task = asyncio.create_task(
                            self._await_gate_and_execute_tool(
                                previous_batch_gate,
                                tool_block,
                                sys_print_callback,
                                turn=turn_count,
                                event_callback=event_callback,
                                semaphore=streaming_semaphore,
                            )
                        )
                        previous_batch_gate = task

                    tool_entries.append({
                        "tool_use_id": tool_id,
                        "task": task,
                    })

                async def stream_status(metrics):
                    await self._emit_stream_event(event_callback, {"type": "stream_status", "turn": turn_count, "metrics": metrics})
                openai_watch = StreamWatch(self.abort_event, stream_status)
                await openai_watch.status("waiting", force=True)
                stream = await openai_watch.wait(self.client.chat.completions.create(**create_kwargs))
                self.active_stream = stream
                openai_watch.metrics["connectedMs"] = openai_watch.ms()
                await openai_watch.status("connected", force=True)
                await self._emit_stream_event(event_callback, {
                    "type": "message_start",
                    "turn": turn_count,
                })

                last_tool_index = -1

                iterator = stream.__aiter__()
                while True:
                    try:
                        chunk = await openai_watch.wait(iterator.__anext__())
                    except StopAsyncIteration:
                        break
                    except (StreamAborted, StreamInterrupted):
                        raise
                    except Exception as exc:
                        raise StreamInterrupted("模型流传输中断，已收到的内容保留。请检查后继续，不会自动重放工具操作。") from exc
                    await openai_watch.chunk(chunk)
                    if self.abort_event.is_set():
                        raise AbortRequestedError("Abort requested during OpenAI streaming response.")
                    chunk_usage = getattr(chunk, "usage", None)
                    if chunk_usage is not None:
                        usage_payload = self._make_usage_block(
                            input_tokens=getattr(chunk_usage, "prompt_tokens", 0) or 0,
                            output_tokens=getattr(chunk_usage, "completion_tokens", 0) or 0,
                        )

                    choices = list(getattr(chunk, "choices", []) or [])
                    if not choices:
                        if openai_watch.terminal_at is not None and chunk_usage is not None:
                            break
                        continue

                    choice = choices[0]
                    delta = getattr(choice, "delta", None)
                    if delta is not None:
                        reasoning = getattr(delta, "reasoning_content", None)
                        if isinstance(reasoning, str):
                            reasoning_fragments.append(reasoning)
                        delta_text = getattr(delta, "content", None)
                        if delta_text:
                            text_fragments.append(delta_text)
                            visible_delta = text_filter.feed(delta_text)
                            if visible_delta:
                                await self._emit_stream_event(event_callback, {"type": "text_delta", "turn": turn_count, "text": visible_delta})
                                await openai_watch.visible()

                        for tool_delta in list(getattr(delta, "tool_calls", []) or []):
                            index = int(getattr(tool_delta, "index", 0) or 0)

                            if index > last_tool_index and last_tool_index >= 0:
                                _dispatch_tool_if_ready(last_tool_index)
                            last_tool_index = max(last_tool_index, index)

                            record = tool_calls.setdefault(index, {
                                "id": "",
                                "name": "",
                                "arguments": [],
                            })
                            tool_id = getattr(tool_delta, "id", None)
                            if tool_id:
                                record["id"] = tool_id
                            fn = getattr(tool_delta, "function", None)
                            if fn is not None:
                                fn_name = getattr(fn, "name", None)
                                fn_arguments = getattr(fn, "arguments", None)
                                if fn_name:
                                    record["name"] = fn_name
                                if fn_arguments:
                                    record["arguments"].append(fn_arguments)
                            if record["id"] and record["name"] and record["id"] not in emitted_tool_ids:
                                emitted_tool_ids.add(record["id"])
                                await self._emit_stream_event(event_callback, {
                                    "type": "tool_scheduled",
                                    "turn": turn_count,
                                    "tool_name": record["name"],
                                    "tool_use_id": record["id"],
                                })

                    if getattr(choice, "finish_reason", None):
                        finish_reason = self._map_openai_finish_reason(choice.finish_reason)
                    if openai_watch.terminal_at is not None and chunk_usage is not None:
                        break

                if openai_watch.terminal_at is None:
                    raise StreamInterrupted("模型连接在收到结束标记前断开，已有内容可能不完整，已保留。请检查后继续，不会自动重放工具操作。")
                await openai_watch.status("completed", force=True)

                full_text = "".join(text_fragments)
                if not tool_calls:
                    try:
                        full_text, recovered = decode_tool_text(full_text, tools_schema)
                    except ToolProtocolError as exc:
                        exc.usage = usage_payload
                        raise
                    for index, call in enumerate(recovered):
                        tool_calls[index] = {**call, "arguments": [call["arguments"]]}
                else:
                    # Native calls take precedence over a duplicate leaked envelope.
                    from xuloom.protocols.dsml import marker_position
                    position = marker_position(full_text)
                    if position is not None:
                        full_text = full_text[:position]
                tail = text_filter.finish(full_text)
                if tail:
                    await self._emit_stream_event(event_callback, {"type": "text_delta", "turn": turn_count, "text": tail})
                if tool_calls:
                    finish_reason = "tool_use"
                for idx in sorted(tool_calls.keys()):
                    _dispatch_tool_if_ready(idx)

                content_blocks = []
                if full_text:
                    content_blocks.append(self._make_text_block(full_text))
                for record in [tool_calls[idx] for idx in sorted(tool_calls.keys())]:
                    tool_id = record.get("id") or f"call_{len(content_blocks) + 1}"
                    raw_arguments = "".join(record.get("arguments", []) or [])
                    argument_error = ""
                    try:
                        parsed_arguments = parse_tool_arguments(raw_arguments)
                    except ToolProtocolError as exc:
                        parsed_arguments, argument_error = {}, str(exc)
                    block = self._make_tool_use_block(
                            tool_id=tool_id,
                            name=record.get("name", ""),
                            input_payload=parsed_arguments,
                        )
                    block._argument_error = argument_error
                    block._raw_arguments = raw_arguments
                    content_blocks.append(block)

                await self._emit_stream_event(event_callback, {
                    "type": "message_stop",
                    "turn": turn_count,
                    "stop_reason": finish_reason,
                })
                from xuloom.core.request_flow import connection_id
                return SimpleNamespace(
                    stop_reason=finish_reason,
                    content=content_blocks,
                    usage=usage_payload,
                    _openai_reasoning=({"connection": connection_id(
                        self.api_base_url or "", self._api_key), "model": model_name, "content": "".join(reasoning_fragments)}
                        if reasoning_fragments else None),
                ), tool_entries, False

            system_blocks = self._build_cached_system_blocks(system_prompt)
            stream_kwargs = {
                "model": model_name,
                "system": system_blocks,
                "messages": api_messages,
                "tools": tools_schema,
                "max_tokens": max_tokens,
            }
            profile = self.reasoning_profile if model_name == self.primary_model else configured_profile(self.model_provider, model_name, self.api_base_url)
            from xuloom.agents.ultra_execution import required
            # Some Claude models reject forced tool use; others skip thinking
            # under it. Keep thinking intact and enforce assessment in the loop.
            if required(self): stream_kwargs['tool_choice'] = {'type':'auto','disable_parallel_tool_use':True}
            if profile is not None:
                stream_kwargs.update(request_options(profile, self.model_provider,
                    effort_override=self.reasoning_effort if model_name == self.primary_model else None, max_tokens=max_tokens))
            elif thinking_config is not None and self._thinking_config_explicit and self._is_native_anthropic():
                stream_kwargs["thinking"] = thinking_config

            try:
                async with self.client.messages.stream(**stream_kwargs) as stream:
                    self.active_stream = stream
                    anthropic_phase = "connected"
                    await self._emit_stream_event(event_callback, {"type": "model_status", "status": "connected", "turn": turn_count})
                    while True:
                        next_event_task = asyncio.create_task(stream.__anext__())
                        abort_wait_task = asyncio.create_task(self.abort_event.wait())
                        done, pending = await asyncio.wait(
                            {next_event_task, abort_wait_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for task in pending:
                            task.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)

                        if abort_wait_task in done and self.abort_event.is_set():
                            next_event_task.cancel()
                            await asyncio.gather(next_event_task, return_exceptions=True)
                            raise AbortRequestedError("Abort requested during streaming response.")

                        try:
                            event = next_event_task.result()
                        except StopAsyncIteration:
                            break

                        event_type = getattr(event, "type", "")
                        if event_type == "message_start":
                            await self._emit_stream_event(event_callback, {
                                "type": "message_start",
                                "turn": turn_count,
                            })
                        elif event_type == "content_block_delta":
                            delta = getattr(event, "delta", None)
                            if getattr(delta, "type", "") == "thinking_delta" and anthropic_phase != "reasoning":
                                anthropic_phase = "reasoning"
                                await self._emit_stream_event(event_callback, {"type": "model_status", "status": "reasoning", "turn": turn_count})
                            if getattr(delta, "type", "") == "text_delta":
                                if anthropic_phase != "receiving":
                                    await self._emit_stream_event(event_callback, {"type": "model_status", "status": "receiving", "turn": turn_count})
                                anthropic_phase = "receiving"
                                await self._emit_stream_event(event_callback, {
                                    "type": "text_delta",
                                    "turn": turn_count,
                                    "text": getattr(delta, "text", ""),
                                })
                        elif event_type == "content_block_stop":
                            content_block = getattr(event, "content_block", None)
                            if getattr(content_block, "type", "") == "tool_use":
                                is_safe = self._is_concurrency_safe(
                                    getattr(content_block, "name", ""),
                                    getattr(content_block, "input", {}) or {},
                                )
                                if is_safe:
                                    if current_safe_batch_tasks is None:
                                        current_safe_batch_gate = previous_batch_gate
                                        current_safe_batch_tasks = []
                                    task = asyncio.create_task(
                                        self._await_gate_and_execute_tool(
                                            current_safe_batch_gate,
                                            content_block,
                                            sys_print_callback,
                                            turn=turn_count,
                                            event_callback=event_callback,
                                            semaphore=streaming_semaphore,
                                            sibling_cancel=streaming_sibling_cancel,
                                        )
                                    )
                                    current_safe_batch_tasks.append(task)
                                else:
                                    if current_safe_batch_tasks is not None:
                                        previous_batch_gate = asyncio.gather(*current_safe_batch_tasks)
                                        current_safe_batch_tasks = None
                                        current_safe_batch_gate = None
                                    task = asyncio.create_task(
                                        self._await_gate_and_execute_tool(
                                            previous_batch_gate,
                                            content_block,
                                            sys_print_callback,
                                            turn=turn_count,
                                            event_callback=event_callback,
                                            semaphore=streaming_semaphore,
                                        )
                                    )
                                    previous_batch_gate = task

                                tool_entries.append({
                                    "tool_use_id": getattr(content_block, "id", None),
                                    "task": task,
                                })
                                await self._emit_stream_event(event_callback, {
                                    "type": "tool_scheduled",
                                    "turn": turn_count,
                                    "tool_name": getattr(content_block, "name", ""),
                                    "tool_use_id": getattr(content_block, "id", None),
                                })

                            await self._emit_stream_event(event_callback, {
                                "type": "content_block_stop",
                                "turn": turn_count,
                                "index": getattr(event, "index", None),
                            })
                        elif event_type == "message_stop":
                            snapshot = stream.current_message_snapshot
                            await self._emit_stream_event(event_callback, {
                                "type": "message_stop",
                                "turn": turn_count,
                                "stop_reason": getattr(snapshot, "stop_reason", None),
                            })

                    final_message = await stream.get_final_message()
                    return final_message, tool_entries, False
            except AbortRequestedError:
                raise
            except Exception as stream_err:
                if self._is_context_overflow_error(stream_err):
                    raise
                await self._cancel_streaming_tool_tasks(tool_entries)
                tool_entries.clear()
                previous_batch_gate = None
                current_safe_batch_gate = None
                current_safe_batch_tasks = None

                disable_fallback = os.environ.get(
                    "XULOOM_DISABLE_NONSTREAMING_FALLBACK", ""
                )
                if disable_fallback or self.network_mode == "auto":
                    raise

                sys_print_callback(
                    "[dim yellow]↳ Streaming request failed mid-stream; "
                    "retrying as non-streaming request.[/dim yellow]"
                )
                await self._emit_stream_event(event_callback, {"type": "model_retry", "message": "流式请求失败，正在重试非流式请求。", "turn": turn_count})
                create_kwargs = {
                    k: v for k, v in stream_kwargs.items()
                }
                response = await self.client.messages.create(**create_kwargs)
                await self._emit_stream_event(event_callback, {
                    "type": "message_start",
                    "turn": turn_count,
                })
                for block in response.content:
                    btype = getattr(block, "type", "")
                    if btype == "text":
                        await self._emit_stream_event(event_callback, {
                            "type": "text_delta",
                            "turn": turn_count,
                            "text": getattr(block, "text", ""),
                        })
                    elif btype == "tool_use":
                        await self._emit_stream_event(event_callback, {
                            "type": "tool_scheduled",
                            "turn": turn_count,
                            "tool_name": getattr(block, "name", ""),
                            "tool_use_id": getattr(block, "id", None),
                        })
                await self._emit_stream_event(event_callback, {
                    "type": "message_stop",
                    "turn": turn_count,
                    "stop_reason": getattr(response, "stop_reason", None),
                })
                return response, [], True
        except StreamAborted:
            await self._cancel_streaming_tool_tasks(tool_entries)
            if openai_watch:
                await openai_watch.status("cancelled", force=True)
            raise AbortRequestedError("Abort requested while receiving model stream.")
        except StreamInterrupted as exc:
            exc.partial_text = text_filter.text[:text_filter.emitted] if self.model_provider == "openai" else ""
            await self._cancel_streaming_tool_tasks(tool_entries)
            exc.tool_results = [entry["task"].result() for entry in tool_entries
                                if entry["task"].done() and not entry["task"].cancelled() and entry["task"].exception() is None]
            if openai_watch:
                openai_watch.metrics["error"] = str(exc)
                await openai_watch.status("error", force=True)
            raise
        finally:
            if self.model_provider == "openai" and self.active_stream is not None:
                close = getattr(self.active_stream, "close", None) or getattr(self.active_stream, "aclose", None)
                if close:
                    try:
                        result = close()
                        if inspect.isawaitable(result):
                            await result
                    except Exception:
                        pass
            self.active_stream = None
            self.active_streaming_tool_entries = []
