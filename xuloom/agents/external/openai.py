"""OpenAI Chat relay for the installed Pi RPC; no second model client or tool loop."""
import asyncio
import copy
import json

from xuloom.core.output_limits import model_reservation, ModelBudgetError
from xuloom.core.run_result import PersistenceError


async def forward(host, value, queue, ready):
    def frame(event):
        return ("data: " + (event if isinstance(event, str) else json.dumps(event, ensure_ascii=False)) + "\n\n").encode()

    async def tools(calls):
        result = []
        for call in calls:
            if call.get("type", "function") != "function": raise ValueError("Uncontrolled tool response")
            function = call["function"]
            block = await host.grant_tool({"id": call["id"], "name": function["name"], "input": json.loads(function["arguments"])})
            result.append({**call, "type": "function", "function": {**function, "arguments": json.dumps(block["input"])}})
        return result

    try:
        with host.budgets():
            field = "max_completion_tokens" if "max_completion_tokens" in value else "max_tokens"
            maximum = min(value[field], getattr(host, "maximum", value[field]))
            async with model_reservation(host.engine, maximum) as grant:
                await host.checkpoint()
                body = copy.deepcopy(value)
                model, messages, streamed = body.pop("model"), body.pop("messages"), body.pop("stream", False)
                body.pop("max_tokens", None); body.pop("max_completion_tokens", None)
                body[field] = grant["maximum"]
                host.reasoning_options(body, grant["maximum"])
                client = host.engine.client.with_options(max_retries=0)
                async with client.chat.completions.with_streaming_response.create(model=model, messages=messages,
                        stream=streamed, extra_body=body) as upstream:
                    ready.set_result((200, "text/event-stream" if streamed else "application/json"))
                    usage = {}
                    if not streamed:
                        data = json.loads(await upstream.text())
                        choices = data.get("choices", [])
                        if len(choices) != 1 or choices[0].get("finish_reason") not in {"stop", "tool_calls"}:
                            raise ValueError("Incomplete provider response")
                        message = choices[0]["message"]
                        if message.get("tool_calls"): message["tool_calls"] = await tools(message["tool_calls"])
                        usage.update(data.get("usage") or {})
                        await queue.put(json.dumps(data).encode())
                    else:
                        pending, finished, stopped, terminal, trailers = {}, False, False, None, []
                        async for line in upstream.iter_lines():
                            if not line.startswith("data:"): continue
                            text = line[5:].strip()
                            if text == "[DONE]":
                                if not finished: raise ValueError("Incomplete provider stream")
                                if pending:
                                    if sorted(pending) != list(range(len(pending))): raise ValueError("Missing tool index")
                                    terminal["choices"][0]["delta"]["tool_calls"] = await tools([pending[index] for index in sorted(pending)])
                                    pending.clear()
                                await queue.put(frame(terminal))
                                for trailer in trailers: await queue.put(frame(trailer))
                                stopped = True
                                break
                            data = json.loads(text)
                            if data.get("error"): raise ValueError("Provider stream failed")
                            usage.update(data.get("usage") or {})
                            choices = data.get("choices", [])
                            if len(choices) > 1: raise ValueError("Multiple choices exceed the owned execution")
                            for choice in choices:
                                if choice.get("index", 0) != 0 or finished: raise ValueError("Changed provider choice")
                                delta = choice.setdefault("delta", {})
                                if delta.get("function_call"): raise ValueError("Uncontrolled legacy tool call")
                                for part in delta.pop("tool_calls", []):
                                    index = part.get("index")
                                    if type(index) is not int or index < 0: raise ValueError("Invalid tool index")
                                    record = pending.setdefault(index, {"index": index, "function": {"name": "", "arguments": ""}})
                                    for key in ("id", "type"):
                                        if key in part:
                                            if key in record and record[key] != part[key]: raise ValueError("Changed tool identity")
                                            record[key] = part[key]
                                    function = part.get("function", {})
                                    for key in ("name", "arguments"):
                                        if key in function: record["function"][key] += function[key]
                                reason = choice.get("finish_reason")
                                if reason is not None:
                                    if reason not in {"stop", "tool_calls"} or (pending and reason != "tool_calls") or (not pending and reason == "tool_calls"):
                                        raise ValueError("Incomplete provider response")
                                    finished = True
                                    terminal = data
                            if not finished: await queue.put(frame(data))
                            elif not choices: trailers.append(data)
                        if not stopped: raise ValueError("Provider stream ended before DONE")
                        await queue.put(frame("[DONE]"))
                    host.usage["input_tokens"] += int(usage.get("prompt_tokens", 0) or 0)
                    host.usage["output_tokens"] += int(usage.get("completion_tokens", 0) or 0)
                    # Missing usage retains the shared reservation's conservative charge.
                    if "completion_tokens" in usage: grant["reported"] = int(usage["completion_tokens"])
    except asyncio.CancelledError:
        if not ready.done(): ready.cancel()
        raise
    except Exception as error:
        host.fail("budget_exhausted" if isinstance(error, ModelBudgetError) else "persistence_error" if isinstance(error, PersistenceError) else "provider_error")
        if not ready.done(): ready.set_result((getattr(error, "status_code", None) or 400, "application/json"))
        else: await queue.put(frame({"error": {"message": "Owned provider stream interrupted", "type": "api_error"}}))
