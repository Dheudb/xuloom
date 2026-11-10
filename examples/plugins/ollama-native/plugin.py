"""Example Xuloom plugin: an Ollama native adapter, a tool, a hook and a Skill.

Install: copy this directory to ``~/.xuloom/plugins/ollama-native`` and run
``xuloom plugins enable ollama-native``. Then select the adapter with
``model_provider: "ollama"`` (base URL defaults to http://127.0.0.1:11434).
Hosts can instead pass the module directly: ``EngineConfig(plugins=[module])``.
"""
import json
import os
import platform

from pydantic import BaseModel

from xuloom.models import ModelAdapter, ToolCall, model_response
from xuloom.tools.base import BaseAgenticTool

NAME = "ollama-native"


class OllamaAdapter(ModelAdapter):
    """Ollama ``/api/chat`` (non-streaming) with native tool calling."""
    name = "ollama"
    default_base_url = "http://127.0.0.1:11434"

    async def stream_turn(self, request):
        messages = []
        for item in request.openai_messages():
            if item.get("tool_calls"):
                # Ollama expects arguments as objects, not JSON strings.
                item = {**item, "tool_calls": [{"function": {"name": call["function"]["name"],
                        "arguments": json.loads(call["function"]["arguments"] or "{}")}} for call in item["tool_calls"]]}
            messages.append({key: value for key, value in item.items() if value is not None})
        body = {"model": request.model, "messages": messages, "stream": False,
                "options": {"num_predict": request.max_tokens}}
        if request.tools:
            body["tools"] = request.openai_tools()
        client = request.client
        response = await client.http.post(client.url("/api/chat"), json=body)
        response.raise_for_status()
        data = response.json()
        message = data.get("message") or {}
        calls = [ToolCall(id=f"ollama_{request.turn}_{index}", name=call["function"]["name"],
                          input=call["function"].get("arguments") or {})
                 for index, call in enumerate(message.get("tool_calls") or [])]
        stop = "tool_use" if calls else ("max_tokens" if data.get("done_reason") == "length" else "end_turn")
        return model_response(text=message.get("content") or "", tool_calls=calls, stop_reason=stop,
                              input_tokens=data.get("prompt_eval_count", 0), output_tokens=data.get("eval_count", 0))

    def is_context_overflow_error(self, error):
        return "context" in str(error).lower() and "exceed" in str(error).lower()


class EmptyInput(BaseModel):
    pass


class SystemInfoTool(BaseAgenticTool):
    name = "system_info"
    description = "Return the operating system, Python version and CPU count of the execution host."
    input_schema = EmptyInput
    is_read_only = True
    risk_level = "low"

    async def execute(self):
        return json.dumps({"os": platform.platform(), "python": platform.python_version(), "cpus": os.cpu_count()})


def register(api):
    api.add_model_adapter(OllamaAdapter())
    api.add_tool(SystemInfoTool())
    # Remind the model of the project rule after every file edit.
    api.add_hook("AfterToolUse", lambda payload: {"context": "Keep CHANGELOG.md updated for user-visible changes."},
                 tools=["file_edit_tool", "file_write_tool"])
    api.add_skills(os.path.join(os.path.dirname(__file__), "skills"))
