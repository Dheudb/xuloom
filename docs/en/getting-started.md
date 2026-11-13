# Getting started

This guide takes you from an empty folder to a running agent with your own tool and plugin, in about ten minutes.

## 1. Install

You need Python 3.10 or newer, and [ripgrep](https://github.com/BurntSushi/ripgrep) (`rg`) on `PATH` for code search.
Xuloom is not on PyPI yet, so install it from GitHub:

```bash
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install "xuloom @ git+https://github.com/Dheudb/xuloom.git"
```

Optional extras: `[mcp]` for MCP servers, `[otel]` for OpenTelemetry export, `[pdf]` for reading PDFs
(PyMuPDF, which is AGPL-3.0 licensed), e.g. `pip install "xuloom[mcp,pdf] @ git+https://github.com/Dheudb/xuloom.git"`.

## 2. Connect a model

Xuloom speaks the OpenAI Chat Completions protocol (which most hosted APIs and local servers such as Ollama,
LM Studio, vLLM and llama.cpp offer) and the Anthropic Messages protocol. For the CLI, set environment variables:

```bash
export OPENAI_API_KEY=sk-...                         # local servers usually accept any value
export OPENAI_MODEL=gpt-4.1                          # the model ID your server expects
export OPENAI_BASE_URL=http://localhost:11434/v1     # only for non-OpenAI endpoints
# or: ANTHROPIC_API_KEY=... ANTHROPIC_MODEL=...
```

In Python, pass the same settings in an `EngineConfig` (next step). The desktop app has a settings dialog instead;
its interface defaults to Chinese and switches to English using the **Language** selector at the top of Settings.

## 3. Run a task

From the shell:

```bash
xuloom exec "List the Python files in this folder and summarize what each does" --permission full
xuloom                      # interactive terminal; /model switches models
xuloom --help               # every command
```

From Python:

```python
import asyncio
from xuloom import ApprovalPolicy, EngineConfig, QueryEngine

config = EngineConfig(model="gpt-4.1", model_provider="openai", api_key="sk-...",
                      api_base_url="https://api.openai.com/v1", cwd=".",
                      approval_policy=ApprovalPolicy("full"))   # "ask" needs a permission handler

async def main():
    async with QueryEngine(config) as engine:
        result = await engine.run("Read README.md and summarize it in three sentences")
        print(result.ok, result.reason)
        print(result.text)

asyncio.run(main())
```

Check `result.ok` and `result.reason` rather than only reading the text: a run can end with `cost_limit`,
`approval_required`, `cancelled` and so on. To watch the run live, iterate `engine.submit_message(prompt)`, which
yields events such as `text_delta`, `tool_scheduled`, `token_usage` (tokens and cost) and one final `final` event;
`engine.set_event_handler(callback)` additionally receives `tool_started` / `tool_result` with arguments and output.

## 4. Write a tool

Any typed function becomes a tool. The docstring is what the model reads, so describe when to use it:

```python
from xuloom import tool

@tool(read_only=True)
def lookup_ticket(ticket_id: str, include_history: bool = False) -> dict:
    """Return the status of an internal ticket.

    Args:
        ticket_id: Ticket identifier such as "T-123".
        include_history: Also return past status changes.
    """
    return {"id": ticket_id, "status": "open"}

config = config.replace(extra_tools=(lookup_ticket,))
```

Tools go through the same input validation, permission checks and receipts as the built-in ones.
`read_only=True` marks tools that change nothing, so they can run in parallel and in plan mode.

## 5. Package it as a plugin

```bash
xuloom plugins new my-tools        # creates ~/.xuloom/plugins/my-tools/
python ~/.xuloom/plugins/my-tools/test_plugin.py      # offline check, no API key needed
xuloom plugins enable my-tools     # the CLI and desktop app load it for new sessions
```

The generated `plugin.py` has an example tool and hook; edit `register(api)` to add your own tools, hooks, model
adapters, skills or CLI commands. See [Extending Xuloom](extending.md) for everything a plugin can add.

## 6. Test without a model

`xuloom.testing.ScriptedModel` plays back scripted responses, so tests need no network or API key:

```python
from xuloom.models import ToolCall, model_response
from xuloom.testing import ScriptedModel

model = ScriptedModel([
    model_response(tool_calls=[ToolCall("1", "lookup_ticket", {"ticket_id": "T-1"})]),
    model_response(text="Ticket T-1 is open."),
])
with model:
    async with QueryEngine(model.config(config)) as engine:
        result = await engine.run("Check ticket T-1")
assert result.ok and len(model.requests) == 2
```

Next: [Extending Xuloom](extending.md).
