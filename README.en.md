# Xuloom

An open-source agent harness for developers: a Python engine you can embed in your own programs. It handles model calls, tool execution, permissions, subagents, context compaction, session persistence and interruption recovery. A desktop workbench and a CLI run on the same engine.

[中文](README.md) · [Documentation](docs/README.md) (Chinese) · [Capabilities](docs/current-capabilities.md) · [Changelog](CHANGELOG.md) · [Windows desktop download](https://github.com/Dheudb/xuloom/releases)

- **Any model**: OpenAI-compatible Chat Completions and Anthropic Messages are built in. Other APIs plug in through a small `ModelAdapter`.
- **Clear extension points**: custom tools, plugins, 13 lifecycle hooks, model adapters and trace exporters all register through the public API, so you never patch the engine.
- **Dependable execution**: every tool call goes through the permission checks and its receipt is saved. Interrupted tasks resume explicitly; operations whose outcome is unknown are never blindly replayed.
- **Visible cost**: run, model and tool spans export to JSONL or OpenTelemetry (Langfuse and others). Requests are priced per model, and a task can have a spending cap.
- **Batteries included**: files, commands, search, browser screenshots, MCP, Skills, parallel subagents, long-context compaction and SSH remote sessions.

## Install

Python 3.10+ is required. Local code search uses [ripgrep](https://github.com/BurntSushi/ripgrep) (`rg`) on PATH.

```bash
pip install xuloom                 # library and CLI
pip install "xuloom[mcp,otel]"     # optional: MCP client, OpenTelemetry export
pip install "xuloom[pdf]"          # optional: PDF reading (PyMuPDF, AGPL-3.0 licensed, ~200 MB)
```

The PyPI release workflow is ready. Until `xuloom` is first published to PyPI, install from source:

```bash
git clone https://github.com/Dheudb/xuloom.git
cd xuloom
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[mcp]"
```

## Embed it

```python
import asyncio
from xuloom import ApprovalPolicy, EngineConfig, QueryEngine

config = EngineConfig(
    model="gpt-4.1", model_provider="openai",          # or "anthropic", or a registered adapter
    api_base_url="https://api.openai.com/v1", api_key="sk-...",
    cwd="./work", approval_policy=ApprovalPolicy("full"),
)

async def main():
    async with QueryEngine(config) as engine:
        result = await engine.run("Read README.md and summarize it in three sentences")
        print(result.ok, result.reason, result.text)

asyncio.run(main())
```

`run()` returns a `RunResult`. Check `ok` and `reason`, not just the text. For streaming events (text deltas, tool calls, token usage and cost), iterate `engine.submit_message(...)`. Every option, event and the recovery semantics are covered in [framework](docs/framework.md) and [extensions](docs/extensions.md).

## Extend it

**Custom tools** declare their input as a Pydantic model. They go through the same validation, approval and receipts as the built-in tools.

```python
from pydantic import BaseModel
from xuloom import BaseAgenticTool

class LookupInput(BaseModel):
    ticket_id: str

class TicketLookup(BaseAgenticTool):
    name = "ticket_lookup"
    description = "Return the status of an internal ticket."
    input_schema = LookupInput
    is_read_only = True

    async def execute(self, ticket_id):
        return f"{ticket_id}: open"

config = config.replace(extra_tools=(TicketLookup(),))
```

**Plugins** bundle tools, hooks, model adapters, Skills, subagents, CLI commands, desktop RPC methods and trace exporters for reuse across projects. The built-in media generation feature is itself a plugin (`xuloom/media/plugin.py`).

```python
def register(api):
    api.add_tool(TicketLookup())
    api.add_hook("BeforeToolUse", block_production_writes, tools=["bash_tool"])
    api.add_model_adapter(MyAdapter())
    api.add_trace_exporter(MyExporter())
```

| Extension point | Purpose | Docs |
| --- | --- | --- |
| `BaseAgenticTool` / `extra_tools` / `disabled_tools` | Add or remove the tools the model sees | [extensions](docs/extensions.md) |
| Plugins (`register(api)`, entry points, `xuloom plugins`) | Package, enable and disable a set of extensions | [plugins](docs/plugins.md) |
| Lifecycle hooks (13 events) | Block or rewrite tool calls, add context before and after tasks, audit | [hooks](docs/hooks.md) |
| `ModelAdapter` | Connect any model API | [model adapters](docs/model-adapters.md) |
| Trace exporters, prices, cost cap | Observability and spend control | [observability](docs/observability.md) |
| `ScriptedModel`, `ReplayModel`, `xuloom eval` | Tests without a real model, offline replay and batch evaluation | [replay and eval](docs/replay-and-eval.md) |
| Skills (`SKILL.md`), subagent definitions (`.md`) | Reusable workflows and roles without code | [capabilities](docs/capabilities.md) |
| MCP servers | External tool servers (stdio / Streamable HTTP, OAuth) | [MCP](docs/mcp.md) |

Complete examples: [examples/extension_project](examples/extension_project/) (custom tool, events, cancellation and resume) and [examples/plugins/ollama-native](examples/plugins/ollama-native/) (an Ollama adapter, a tool, a hook and a Skill).

## CLI

```bash
xuloom                                                        # interactive terminal
xuloom exec "fix the failing tests" --cwd ./repo --json      # non-interactive: JSON events, exit code = outcome
xuloom plugins list
xuloom serve --root ./work                                    # HTTP + SSE API, see docs/server.md
```

`exec` exit codes: 0 completed, 3 waiting for approval or input, 8 budget or cost cap exhausted. The rest are listed in [CLI](docs/cli.md).

## Desktop workbench

The Windows x64 desktop app runs the same engine and ships its own Python runtime. Download `Xuloom-v<version>-windows-x64.zip` from [Releases](https://github.com/Dheudb/xuloom/releases), extract the whole archive and run `Xuloom.exe`. Then enter your API endpoint, key and model under **Settings → Model connections**. There is no desktop build for Linux or macOS yet, and no signed installer or auto-update; on those platforms use the library and the CLI.

## Experimental

These work but may still change; each document states what has been verified:

- **[Scaling](docs/scaling.md)**: a separate workbench running grouped workers on this machine and SSH nodes. It is off by default and requires explicit confirmation of high usage.
- **[External agent backends](docs/external-agents.md)**: Claude Code, Pi, Codex and DSH as controlled subagents.
- **[Native SSH workers](docs/history/distributed-workers-plan.md)**: subagents running on configured SSH hosts.

## Contributing

```bash
pip install -e ".[dev,mcp,pdf]"
ruff check . && mypy && python -m unittest discover -s tests
```

See [AGENTS.md](AGENTS.md) and [CONTRIBUTING.md](CONTRIBUTING.md). Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
