"""Responses transport boundary for an installed Codex; execution stays native."""
import asyncio
import copy
import json

from xuloom.core.output_limits import model_reservation, ModelBudgetError
from xuloom.core.run_result import PersistenceError
from .host import PREFIX, SOURCE_ID, LIMIT


def native_name(item):
    name, namespace = item.get("name"), item.get("namespace")
    if not isinstance(name, str): raise ValueError("Missing Responses tool name")
    if namespace is not None:
        if namespace != "mcp__xuloom": raise ValueError("Uncontrolled Responses namespace")
        return PREFIX + name
    return name


def payload(host, value):
    if not isinstance(value, dict) or value.get("model") != host.engine.primary_model or value.get("stream") is not True:
        raise ValueError("Changed Responses model or transport")
    if value.get("previous_response_id") or value.get("conversation") or value.get("store") is not False:
        raise ValueError("Only owned, locally persisted Responses history is supported")
    if not isinstance(value.get("tools", []), list) or not isinstance(value.get("input"), list):
        raise ValueError("Invalid Responses input")
    if value.get("tool_choice", "auto") not in {"auto", "none", "required"}:
        raise ValueError("An external tool selection cannot override native tool visibility")
    body = copy.deepcopy(value)
    # Codex has native tools that its current feature flags cannot all disable.
    # Do not expose them upstream, and validate every returned call below before
    # releasing its completion event. Hiding alone is not authorization.
    visible = []
    for tool in body.get("tools", []):
        if not isinstance(tool, dict): raise ValueError("Invalid Responses tool definition")
        if tool.get("type") == "namespace" and tool.get("name") == "mcp__xuloom":
            eligible = [entry for entry in tool.get("tools", []) if isinstance(entry, dict) and entry.get("type") == "function" and entry.get("name") in host.tools()]
            if eligible: visible.append({**tool,"tools":eligible})
        elif tool.get("type") == "function" and isinstance(tool.get("name"), str) and tool["name"].startswith(PREFIX) and tool["name"][len(PREFIX):] in host.tools():
            visible.append(tool)
    body["tools"] = visible
    for item in body["input"]:
        if not isinstance(item, dict) or item.get("type", "message") not in {"message", "reasoning", "function_call", "function_call_output"}:
            raise ValueError("Uncontrolled Responses history item")
        if item.get("type") == "function_call":
            if not native_name(item).startswith(PREFIX):
                raise ValueError("Uncontrolled historical tool call")
            arguments = json.loads(item["arguments"])
            if not isinstance(arguments, dict): raise ValueError("Invalid historical tool arguments")
            arguments.pop(SOURCE_ID, None); item["arguments"] = json.dumps(arguments)
    return body


def reasoning(host, body, maximum):
    profile = host.engine.reasoning_profile
    if profile is not None and profile.get("adapter") != "openai":
        raise ValueError("Codex requires Responses reasoning controls; this connection declares a different format")
    if profile is not None:
        from xuloom.core.reasoning import request_options
        options = request_options(profile, "openai", effort_override=host.engine.reasoning_effort, max_tokens=maximum)
        body.pop("reasoning", None)
        if options: body["reasoning"] = {"effort": options["reasoning_effort"]}
    elif host.engine.reasoning_effort:
        body["reasoning"] = {"effort": host.engine.reasoning_effort}


def safe_item(item):
    if not isinstance(item, dict) or item.get("type") not in {"message", "reasoning", "function_call"}:
        raise ValueError("Provider returned a hosted or uncontrolled tool")
    if item["type"] == "message" and (item.get("role") != "assistant" or
            any(not isinstance(block, dict) or block.get("type") not in {"output_text", "refusal"} for block in item.get("content", []))):
        raise ValueError("Invalid Responses assistant item")


async def forward(host, value, queue, ready):
    def frame(event):
        return ("event: " + event["type"] + "\ndata: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode()
    try:
        with host.budgets():
            requested = value.get("max_output_tokens", host.maximum)
            if type(requested) is not int or requested <= 0: raise ValueError("Invalid Responses output grant")
            async with model_reservation(host.engine, min(requested, host.maximum)) as grant:
                await host.checkpoint()
                body = copy.deepcopy(value); body["max_output_tokens"] = grant["maximum"]
                reasoning(host, body, grant["maximum"])
                model, inputs = body.pop("model"), body.pop("input"); body.pop("stream", None)
                client = host.engine.client.with_options(max_retries=0)
                async with client.responses.with_streaming_response.create(model=model, input=inputs, stream=True, extra_body=body) as upstream:
                    ready.set_result((200, "text/event-stream"))
                    pending, terminal, buffered = {}, False, 0
                    async for line in upstream.iter_lines():
                        if len(line.encode()) > LIMIT: raise ValueError("Responses frame exceeds the protocol limit")
                        if not line.startswith("data:"): continue
                        event = json.loads(line[5:].strip())
                        if not isinstance(event, dict): raise ValueError("Invalid Responses event")
                        kind = event.get("type", "")
                        if kind in {"error", "response.failed", "response.incomplete"}: raise ValueError("Incomplete Responses execution")
                        if kind not in {"response.created", "response.in_progress", "response.completed", "response.output_item.added", "response.output_item.done",
                                "response.content_part.added", "response.content_part.done", "response.output_text.delta", "response.output_text.done", "response.refusal.delta", "response.refusal.done",
                                "response.reasoning_summary_part.added", "response.reasoning_summary_part.done", "response.reasoning_summary_text.delta", "response.reasoning_summary_text.done",
                                "response.reasoning_text.delta", "response.reasoning_text.done", "response.function_call_arguments.delta", "response.function_call_arguments.done"}:
                            raise ValueError("Unsupported Responses event cannot introduce an execution path")
                        if kind in {"response.created", "response.in_progress"} and event.get("response", {}).get("output"):
                            raise ValueError("A response cannot publish tools before its completion")
                        index = event.get("output_index")
                        if kind == "response.output_item.added":
                            item = event.get("item"); safe_item(item)
                            if item["type"] == "function_call":
                                if type(index) is not int or index < 0 or index in pending: raise ValueError("Changed tool index")
                                pending[index] = {"item": item, "arguments": "", "done": False}; continue
                        if kind.startswith("response.function_call_arguments."):
                            if index not in pending: raise ValueError("Tool argument event has no owned item")
                            record = pending[index]
                            if event.get("item_id") != record["item"].get("id") or record["done"]:
                                raise ValueError("Tool arguments changed identity or arrived after completion")
                            if kind.endswith(".delta"):
                                delta = event.get("delta")
                                if not isinstance(delta, str): raise ValueError("Invalid tool argument fragment")
                                buffered += len(delta.encode()); record["arguments"] += delta
                                if buffered > LIMIT: raise ValueError("External tool arguments exceed the protocol limit")
                            elif kind.endswith(".done"):
                                if record["arguments"] and record["arguments"] != event.get("arguments"): raise ValueError("Tool arguments changed")
                                arguments = event.get("arguments")
                                if not isinstance(arguments, str): raise ValueError("Missing completed tool arguments")
                                buffered += max(0, len(arguments.encode()) - len(record["arguments"].encode()))
                                if buffered > LIMIT: raise ValueError("External tool arguments exceed the protocol limit")
                                record["arguments"] = arguments
                            else: raise ValueError("Unknown tool argument event")
                            continue
                        if kind == "response.output_item.done":
                            item = event.get("item"); safe_item(item)
                            if item["type"] == "function_call":
                                record = pending.get(index)
                                if not record or record["done"] or any(record["item"].get(key) != item.get(key) for key in ("id", "call_id", "name", "namespace")):
                                    raise ValueError("Tool completion changed identity")
                                if record["arguments"] and record["arguments"] != item.get("arguments"): raise ValueError("Incomplete tool arguments")
                                arguments = item.get("arguments")
                                if not isinstance(arguments, str): raise ValueError("Missing tool arguments")
                                buffered += max(0, len(arguments.encode()) - len(record["arguments"].encode()))
                                if buffered > LIMIT: raise ValueError("External tool arguments exceed the protocol limit")
                                record.update(item=item, done=True); continue
                        if kind == "response.completed":
                            response = event.get("response", {})
                            if response.get("status") != "completed" or not isinstance(response.get("output"), list): raise ValueError("Missing Responses completion")
                            for item in response["output"]: safe_item(item)
                            expected = {record["item"].get("id") for record in pending.values()}
                            actual = {item.get("id") for item in response["output"] if isinstance(item, dict) and item.get("type") == "function_call"}
                            if expected != actual or len(expected) != len(pending) or len(actual) != sum(item.get("type") == "function_call" for item in response["output"]):
                                raise ValueError("Responses completion omitted, duplicated or invented a tool")
                            usage = response.get("usage") or {}
                            if not isinstance(usage, dict) or any(type(usage.get(key, 0)) is not int or usage.get(key, 0) < 0 for key in ("input_tokens", "output_tokens")):
                                raise ValueError("Invalid Responses usage receipt")
                            released = []
                            for index, record in pending.items():
                                item = record["item"]
                                if not record["done"] or item not in response["output"]: raise ValueError("Unconfirmed Responses tool call")
                                caller = item.get("caller") or {}
                                if not isinstance(item.get("id"),str) or not item["id"] or item.get("status") not in {None,"completed"} or item.get("async") or not isinstance(caller, dict) or caller.get("type","direct") != "direct":
                                    raise ValueError("Unsupported asynchronous or incomplete tool execution")
                                granted = await host.grant_tool({"id": item["call_id"], "name": native_name(item), "input": json.loads(item["arguments"])})
                                item["arguments"] = json.dumps(granted["input"])
                                response["output"] = [item if value.get("id") == item["id"] else value for value in response["output"]]
                                released.extend((
                                    {"type":"response.output_item.added", "output_index":index, "item":{**item,"arguments":"","status":"in_progress"}},
                                    {"type":"response.function_call_arguments.delta", "output_index":index,"item_id":item["id"],"delta":item["arguments"]},
                                    {"type":"response.function_call_arguments.done", "output_index":index,"item_id":item["id"],"arguments":item["arguments"]},
                                    {"type":"response.output_item.done", "output_index":index,"item":item},
                                ))
                            # A later invalid call/checkpoint cannot leave an
                            # earlier call in this response already executing.
                            host.usage["input_tokens"] += int(usage.get("input_tokens", 0) or 0)
                            host.usage["output_tokens"] += int(usage.get("output_tokens", 0) or 0)
                            if "output_tokens" in usage: grant["reported"] = int(usage["output_tokens"])
                            for output in released: await queue.put(frame(output))
                            await queue.put(frame(event)); terminal = True; break
                        await queue.put(frame(event))
                    if not terminal: raise ValueError("Responses stream ended before completion")
    except asyncio.CancelledError:
        if not ready.done(): ready.cancel()
        raise
    except Exception as error:
        host.fail("budget_exhausted" if isinstance(error, ModelBudgetError) else "persistence_error" if isinstance(error, PersistenceError) else "provider_error")
        if not ready.done(): ready.set_result((getattr(error, "status_code", None) or 400, "application/json"))
        else: await queue.put(frame({"type":"error","error":{"message":"Owned Responses stream interrupted"}}))
