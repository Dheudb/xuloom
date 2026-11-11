# Extending Xuloom

Everything here goes through the public API (`from xuloom import ...`); nothing requires patching the engine.
The stable surface is what `xuloom/__init__.py` exports; `engine.tool_context` and `_`-prefixed members are internal.

## Tools

The quickest way is a typed function with `@tool` (see [Getting started](getting-started.md#4-write-a-tool)):

| Option | Meaning |
| --- | --- |
| `name=` | Tool name shown to the model (default: the function name; letters, digits, `_`, `-`) |
| `description=` | Overrides the docstring's first paragraph |
| `read_only=True` | The tool changes nothing: allowed in plan mode and run concurrently; default risk level `low` |
| `risk_level=` | `low`, `medium` or `high` (default `high` unless read-only), shown in approval prompts |
| `workspace_paths=` | `{"path_argument": default_to_workspace}`: resolve these arguments inside the workspace |

Parameter types can be anything Pydantic accepts (`Literal[...]`, `list[str]`, `Optional[...]`,
`Annotated[int, Field(ge=1)]`, nested models). Invalid model input is rejected before your function runs.
A parameter named `context` receives the engine's tool context (for example `context["session_cwd"]`).
Sync functions run in a worker thread; non-string results are sent to the model as JSON.

For full control (custom permission summaries, concurrency rules, input repair hints), subclass
`BaseAgenticTool` and set `name`, `description`, `input_schema` and `async def execute(self, **arguments)`.
`@tool` returns such a subclass. Pass classes or instances in `EngineConfig(extra_tools=...)`; a class is
instantiated once per engine. `disabled_tools={"bash_tool", ...}` removes built-in tools.

## Plugins

A plugin is a module, object or function with `register(api)`; it is called once per engine:

```python
NAME = "my-plugin"

def register(api):
    api.add_tool(lookup_ticket)                         # @tool function or BaseAgenticTool
    api.add_tool(rare_tool, latent=True)                # hidden until the model finds it via tool_search_tool
    api.add_hook("BeforeToolUse", guard, tools=["bash_tool"])
    api.add_model_adapter(MyAdapter())
    api.add_skills(Path(__file__).parent / "skills")    # <dir>/<skill>/SKILL.md
    api.add_agents(Path(__file__).parent / "agents")    # <dir>/<role>.md subagent definitions
    api.add_cli_command("my-cmd", configure, handler)   # xuloom my-cmd ...
    api.add_rpc_method("my-plugin.status", handler)     # callable from the desktop app
    api.add_trace_exporter(MyExporter())                # receives run/model/tool spans
```

Install a plugin as a Python package (entry point group `xuloom.plugins`) or as a folder
`~/.xuloom/plugins/<name>/plugin.py` (what `xuloom plugins new` creates), then `xuloom plugins enable <name>`.
Hosts embedding Xuloom pass `EngineConfig(plugins=[module_or_name])`, or `load_enabled_plugins=True` to apply
the user's enabled list. Registration is all-or-nothing: if `register` fails, nothing from that plugin is installed.
The bundled media-generation feature is a plugin too: `xuloom/media/plugin.py`.

## Lifecycle hooks

`engine.add_hook(event, callback, tools=None, modes=None)` or `api.add_hook(...)`. Callbacks receive a copy of the
payload (always `event`, `session_id`, `agent_id`, `agent_role`, `cwd`, `mode`, plus event fields) and return
`None` or a dict with `decision`, `reason`, `context` (added to the conversation as a reminder) and, for
`BeforeToolUse` only, `updated_input`.

| Event | When | Can also |
| --- | --- | --- |
| `SessionStart` | First run of a session | |
| `UserPromptSubmit` | A new task is about to be sent | `block` |
| `BeforeToolUse` | Before a tool runs | `reject`, `updated_input` |
| `AfterToolUse` | After a tool result is saved | |
| `PostSampling` | After the final answer of a turn is sampled | |
| `Stop` | Before the final answer is accepted | `block`, `retry` |
| `TurnEnd` / `TurnError` | A turn finished / failed or was cancelled | |
| `PreCompact` / `PostCompact` | Around context compaction | |
| `SubagentStart` / `SubagentStop` | A subagent was created / finished | |
| `SessionEnd` | The engine is closing | |

```python
def guard(payload):
    if payload["tool_input"].get("command", "").startswith("rm -rf"):
        return {"decision": "reject", "reason": "recursive deletes are not allowed"}
```

A `BeforeToolUse` callback that raises rejects the tool call (fail closed); other hooks only log the error.
Hooks can also be configured without code in `<workspace>/.xuloom/hooks.json` (templates or shell commands).

## Model adapters

Built in: `openai` (Chat Completions compatible, plus Responses for official models) and `anthropic` (Messages).
Anything else needs a `ModelAdapter`:

```python
from xuloom.models import ModelAdapter, ToolCall, model_response, register_adapter

class MyAdapter(ModelAdapter):
    name = "my-api"
    default_base_url = "https://api.example.com"

    async def stream_turn(self, request):
        body = {"model": request.model, "messages": request.openai_messages(),
                "tools": request.openai_tools(), "max_tokens": request.max_tokens}
        reply = (await request.client.http.post(request.client.url("/v1/chat"), json=body)).json()
        calls = [ToolCall(c["id"], c["name"], c["arguments"]) for c in reply.get("tool_calls", [])]
        return model_response(text=reply.get("text", ""), tool_calls=calls,
                              input_tokens=reply["usage"]["input"], output_tokens=reply["usage"]["output"])

register_adapter(MyAdapter())
config = EngineConfig(model="m1", model_provider="my-api", api_key="...")
```

Optional members: `create_client`, `complete_text` (summaries; defaults to `stream_turn`), `is_transient_error`
(retry on the fallback model) and `is_context_overflow_error` (compact and retry). A complete example for
Ollama's native API is in `examples/plugins/ollama-native/plugin.py`.

## Tracing and cost

```python
from xuloom import JsonlTraceExporter
config = EngineConfig(trace_exporters=(JsonlTraceExporter("trace.jsonl"),),
                      model_prices={"gpt-4.1": {"input": 2, "output": 8}},   # USD per million tokens
                      max_cost_usd=0.50)                                       # per task, subagents included
```

Each task produces a `run` span with `model` and `tool` children (OpenTelemetry GenAI attribute names).
`XULOOM_TRACE_FILE=path.jsonl` and `XULOOM_TRACE_OTLP=1` (OpenTelemetry/Langfuse, needs `[otel]`) work without code.
Prices can also live in `~/.xuloom/pricing.json`; patterns like `"claude-sonnet-*"` are allowed. Local models that
cost nothing should be priced `{"input": 0, "output": 0}`: with a cap set, an unpriced model is refused rather than
run without limit. When a server returns no usage, tokens are estimated locally; the model span's
`xuloom.usage.source` is then the estimator (for example `tiktoken` or `heuristic`) instead of `api_usage`.

## Replay and evals

- Record a real run with `EngineConfig(record_responses="run.jsonl")`, then replay it offline with
  `xuloom.testing.ReplayModel("run.jsonl")`; tools still run for real, so this tests your harness code.
- `xuloom eval cases.json` runs each case in a fresh workspace and checks the outcome (`ok`, `contains`,
  `file_contains`, `tool_called`, `max_cost_usd` ...); it exits with 10 when a case fails.

## HTTP server

`xuloom serve --root ./work` exposes sessions over HTTP with Server-Sent Events, remote approvals and a bearer
token (`XULOOM_SERVER_TOKEN`); it listens on localhost by default. Embed it with `xuloom.server.XuloomServer`.
Endpoints: `POST /v1/sessions`, `POST /v1/sessions/{id}/messages` (`"stream": true` for SSE),
`POST /v1/sessions/{id}/approvals/{request_id}`, `POST /v1/sessions/{id}/abort`, `DELETE /v1/sessions/{id}`.
