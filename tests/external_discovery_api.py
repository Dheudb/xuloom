"""Owned model wire fixtures; installed vendor CLIs still perform real tool calls."""
import json


class CatalogModel:
    def __init__(self, provider="anthropic"):
        self.provider, self.catalogs = provider, []

    def __call__(self, handler, body):
        if handler.path.endswith("/messages/count_tokens"):
            return self.json(handler, {"input_tokens": 500})
        if body.get("stream") is not True:
            return self.reply(handler, body, text="Fixture summary: preserve worker identity and tool results.")
        names = [tool["function"]["name"] if self.provider == "openai" else tool["name"] for tool in body.get("tools", [])]
        self.catalogs.append(names)
        step = len(self.catalogs); final = step >= 3 and step % 2 == 1
        name = "mcp__xuloom__tool_search_tool" if step == 1 else "mcp__xuloom__task_list_tool"
        args = {"query": "list background tasks", "activate": ["task_list_tool"]} if step == 1 else {}
        return self.reply(handler, body, name=None if final else name, values=args,
            text="CATALOG_COMPLETE" if step == 3 else "FOLLOWUP_COMPLETE", identity="catalog_" + str(step))

    @staticmethod
    def json(handler, value):
        raw = json.dumps(value).encode()
        handler.send_response(200); handler.send_header("Content-Type", "application/json"); handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers(); handler.wfile.write(raw)
        return True

    def reply(self, handler, body, *, name=None, values=None, text="", identity="fixture_summary"):
        final = name is None
        if body.get("stream") is not True:
            usage = {"input_tokens": 10, "output_tokens": 5}
            if self.provider == "openai":
                value = {"id": identity, "object": "chat.completion", "created": 0, "model": body["model"],
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
            else:
                value = {"id": identity, "type": "message", "role": "assistant", "model": body["model"],
                    "content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "stop_sequence": None, "usage": usage}
            return self.json(handler, value)
        if self.provider == "openai":
            def chunk(delta, reason=None):
                return {"id": identity, "object": "chat.completion.chunk", "created": 0, "model": body["model"],
                    "choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}
            delta = {"role": "assistant", "content": text} if final else {"role": "assistant", "tool_calls": [{"index": 0,
                "id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(values or {})}}]}
            events = [chunk(delta), chunk({}, "stop" if final else "tool_calls"), {"id": identity, "object": "chat.completion.chunk",
                "created": 0, "model": body["model"], "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}]
            raw = ("".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n").encode()
        else:
            block = {"type": "text", "text": ""} if final else {"type": "tool_use", "id": identity, "name": name, "input": {}}
            delta = {"type": "text_delta", "text": text} if final else {"type": "input_json_delta", "partial_json": json.dumps(values or {})}
            events = [{"type": "message_start", "message": {"id": identity, "type": "message", "role": "assistant", "model": body["model"],
                "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 0}}},
                {"type": "content_block_start", "index": 0, "content_block": block}, {"type": "content_block_delta", "index": 0, "delta": delta},
                {"type": "content_block_stop", "index": 0}, {"type": "message_delta", "delta": {"stop_reason": "end_turn" if final else "tool_use", "stop_sequence": None},
                    "usage": {"output_tokens": 5}}, {"type": "message_stop"}]
            raw = "".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n" for event in events).encode()
        handler.send_response(200); handler.send_header("Content-Type", "text/event-stream"); handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers(); handler.wfile.write(raw)
        return True
