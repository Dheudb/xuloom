"""Explicit Responses transport and opaque native compaction; no Chat fallback."""
import asyncio
import copy
import json
from types import SimpleNamespace

from xuloom.context.compaction import ContextLimitError, route
from xuloom.core.messages import create_user_message, get_msg_uuid
from xuloom.core.output_limits import model_reservation
from xuloom.protocols.dsml import parse_tool_arguments
from xuloom.protocols.stream_watch import StreamWatch, StreamInterrupted


def plain(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def blocks(content):
    if isinstance(content, str): return [{"type": "input_text", "text": content}]
    result = []
    for block in content if isinstance(content, list) else []:
        kind = block.get("type")
        if kind == "text": result.append({"type": "input_text", "text": block.get("text", "")})
        elif kind == "image":
            source = block.get("source", {})
            url = source.get("url") if source.get("type") == "url" else "data:" + source.get("media_type", "image/png") + ";base64," + source.get("data", "")
            result.append({"type": "input_image", "image_url": url})
        elif kind == "document": raise ContextLimitError("Native Responses document input is not declared by this adapter")
    return result


def convert(messages):
    items = []
    for message in messages:
        if message.get("_responses_output"):
            items.extend(copy.deepcopy(message["_responses_output"]))
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            if content: items.append({"role": message.get("role", "user"), "content": content})
            continue
        for block in content if isinstance(content, list) else []:
            kind = block.get("type")
            if kind == "tool_use":
                items.append({"type": "function_call", "call_id": block["id"], "name": block["name"], "arguments": plain(block.get("input", {}))})
            elif kind == "tool_result":
                value = block.get("content", "")
                text = [v for v in value if v.get("type") != "image"] if isinstance(value, list) else value
                items.append({"type": "function_call_output", "call_id": block["tool_use_id"], "output": plain(text)})
                images = blocks([v for v in value if v.get("type") == "image"]) if isinstance(value, list) else []
                if images: items.append({"role": "user", "content": images})
            elif kind == "text" and block.get("text"):
                items.append({"role": message.get("role", "user"), "content": block["text"]})
            elif kind in {"image", "document"}:
                items.append({"role": "user", "content": blocks([block])})
    return items


def native_tail(engine, native, messages=None):
    if native.get("version") != 1 or type(native.get("tokens")) is not int or native["tokens"] <= 0 or not isinstance(native.get("anchor"), str):
        raise ContextLimitError("Invalid saved native compaction checkpoint")
    validate_output(native.get("items"), compaction=True)
    if native.get("route") != route(engine): raise ContextLimitError("Native compacted state belongs to a different model route")
    found = [index for index, message in enumerate(engine.messages) if get_msg_uuid(message) == native.get("anchor")]
    if len(found) != 1: raise ContextLimitError("Native compaction anchor is missing or ambiguous; original archive retained")
    return engine.messages[found[0]+1:]


def inputs(engine, messages):
    native = getattr(engine, "native_context", None)
    if native: return copy.deepcopy(native["items"]) + convert(native_tail(engine, native))
    return convert(messages)


def dump(value):
    return value.model_dump(mode="json", exclude_none=True) if hasattr(value, "model_dump") else copy.deepcopy(value)


def validate_output(items, *, compaction=False):
    if not isinstance(items, list) or not items: raise ContextLimitError("Provider returned no canonical Responses output")
    for item in items:
        if not isinstance(item, dict) or item.get("type") not in {"message", "reasoning", "function_call", "function_call_output", "compaction"}:
            raise ContextLimitError("Provider returned an undeclared hosted tool or invalid Responses item")
        if item["type"] == "compaction" and (not compaction or not isinstance(item.get("encrypted_content"), str) or not item["encrypted_content"]):
            raise ContextLimitError("Invalid opaque compaction item")


async def compact_native(engine, messages, spec, focus):
    from xuloom.core.output_limits import active_budgets
    from xuloom.features.goals import available_output
    budgets = active_budgets(engine.tool_context.get("goal_budget"))
    if any(available_output(budget) < float("inf") or budget.get("request_cap", float("inf")) < float("inf") for budget in budgets) or engine.tool_context.get("acquire_model_budget"):
        raise ContextLimitError("Native compaction cannot enforce a finite output grant; use text summary for budgeted execution")
    input_items = inputs(engine, messages)
    # No output-cap parameter exists on /responses/compact. Reserve conservatively.
    async with model_reservation(engine, spec["window"]) as grant:
        if grant["maximum"] < spec["window"]:
            raise ContextLimitError("Native compaction cannot enforce this finite output grant; use text summary for budgeted execution")
        task = asyncio.create_task(engine.client.responses.compact(model=engine.primary_model, input=input_items,
            instructions="Preserve user goals, constraints, evidence references, tool identities and unfinished work. " + focus[:4000]))
        aborted = asyncio.create_task(engine.abort_event.wait())
        try:
            done, _ = await asyncio.wait({task, aborted}, return_when=asyncio.FIRST_COMPLETED)
            if aborted in done and engine.abort_event.is_set(): raise asyncio.CancelledError("Native compaction cancelled")
            response = await task
        finally:
            for work in (task, aborted):
                if not work.done(): work.cancel()
            await asyncio.gather(task, aborted, return_exceptions=True)
        raw = dump(response)
        output = raw.get("output")
        validate_output(output, compaction=True)
        if not any(item.get("type") == "compaction" for item in output): raise ContextLimitError("Provider did not return native compacted state")
        usage = raw.get("usage") or {}
        grant["reported"] = int(usage.get("output_tokens", 0) or 0)
        engine._record_auxiliary_usage("native_compaction", usage)
        anchor = create_user_message("[CompactionBoundary:native_responses]\nNative context retained; this marker grants no authorization.", is_meta=True, origin="compaction")
        tokens = int(usage.get("output_tokens", 0) or 0) or engine.compactor.estimate_tokens([{"role": "user", "content": plain(output)}])
        if tokens <= 0: raise ContextLimitError("Invalid native token count")
        state = dict(version=1, route=route(engine), anchor=get_msg_uuid(anchor), items=output, tokens=tokens)
        return copy.deepcopy(engine.messages) + [anchor], state


async def receive(engine, *, system_prompt, api_messages, tools_schema, turn_count, model_name, max_tokens,
        thinking_config=None, sys_print_callback=print, event_callback=None):
    if model_name != engine.primary_model: raise ContextLimitError("Native Responses routing cannot silently switch models")
    from xuloom.core.model_inputs import validate_media
    validate_media(api_messages, engine._input_contract(model_name))
    tools = [{"type": "function", **tool.get("function", tool)} for tool in tools_schema]
    options = dict(model=model_name, instructions=system_prompt, input=inputs(engine, engine._get_messages_after_compact_boundary()), tools=tools,
        stream=True, store=False, max_output_tokens=max_tokens, include=["reasoning.encrypted_content"])
    if engine.reasoning_profile:
        from xuloom.core.reasoning import request_options
        if engine.reasoning_profile["adapter"] != "openai": raise ContextLimitError("Responses requires declared OpenAI reasoning controls")
        effort = request_options(engine.reasoning_profile, "openai", effort_override=engine.reasoning_effort, max_tokens=max_tokens).get("reasoning_effort")
        if effort: options["reasoning"] = {"effort": effort}
    elif engine.reasoning_effort: options["reasoning"] = {"effort": engine.reasoning_effort}
    async def emit(event): await engine._emit_stream_event(event_callback, event)
    async def stream_status(value): await emit({"type": "model_stream", "turn": turn_count, **value})
    watch = StreamWatch(engine.abort_event, stream_status)
    visible = []
    response, stream = None, None
    try:
        stream = await watch.wait(engine.client.responses.create(**options))
        engine.active_stream = stream
        await emit({"type": "message_start", "turn": turn_count})
        iterator = stream.__aiter__()
        while True:
            try: event = await watch.wait(iterator.__anext__())
            except StopAsyncIteration: break
            item = dump(event)
            kind = item.get("type")
            delta = SimpleNamespace(content=item.get("delta") if kind == "response.output_text.delta" else None,
                reasoning_content=item.get("delta") if kind == "response.reasoning_summary_text.delta" else None,
                tool_calls=[item] if kind in {"response.function_call_arguments.delta", "response.output_item.done"} else [])
            await watch.chunk(SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason="stop" if kind == "response.completed" else None)]))
            if kind == "response.output_text.delta":
                text = item.get("delta", "")
                visible.append(text)
                await watch.visible()
                await emit({"type": "text_delta", "turn": turn_count, "text": text})
            elif kind == "response.reasoning_summary_text.delta":
                await emit({"type": "thinking_delta", "turn": turn_count, "text": item.get("delta", "")})
            elif kind == "response.completed": response = item.get("response"); break
            elif kind in {"response.failed", "response.incomplete", "error"}:
                raise ContextLimitError("Responses request did not complete; existing effects retained")
        if not response or response.get("status") != "completed": raise ContextLimitError("Responses stream ended without a completion receipt")
        output = response.get("output")
        validate_output(output)
        content = []
        for item in output:
            if item["type"] == "message":
                if item.get("role") != "assistant": raise ContextLimitError("Invalid provider assistant role")
                for block in item.get("content", []):
                    if block.get("type") in {"output_text", "refusal"}: content.append(engine._make_text_block(block.get("text", block.get("refusal", ""))))
                    else: raise ContextLimitError("Undeclared Responses output content")
            elif item["type"] == "function_call":
                arguments = parse_tool_arguments(item.get("arguments", ""))
                content.append(engine._make_tool_use_block(tool_id=item["call_id"], name=item["name"], input_payload=arguments))
            elif item["type"] != "reasoning": raise ContextLimitError("Unexpected Responses output type")
        if not content: raise ContextLimitError("Responses returned no visible answer or callable tool; history retained")
        usage = response.get("usage") or {}
        stop = "tool_use" if any(item["type"] == "function_call" for item in output) else "end_turn"
        await emit({"type": "message_stop", "turn": turn_count, "stop_reason": stop})
        return SimpleNamespace(content=content, stop_reason=stop, responses_output=output,
            usage=engine._make_usage_block(input_tokens=usage.get("input_tokens", 0), output_tokens=usage.get("output_tokens", 0))), [], False
    except StreamInterrupted as error:
        error.partial_text = "".join(visible)
        raise
    finally:
        engine.active_stream = None
        if stream: await stream.close()
