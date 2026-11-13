# Xuloom

An open-source agent harness for developers: a Python engine you can embed in your own programs. It handles model calls, tool execution, permissions, subagents, context compaction, session persistence and interruption recovery. A desktop workbench and a CLI run on the same engine.

[中文](README.md) · [Getting started](docs/en/getting-started.md) · [Extending](docs/en/extending.md) · [All docs](docs/README.md) (Chinese) · [Capabilities](docs/current-capabilities.md) · [Changelog](CHANGELOG.md) · [Windows desktop download](https://github.com/Dheudb/xuloom/releases)

- **Any model**: OpenAI-compatible Chat Completions and Anthropic Messages are built in. Other APIs plug in through a small `ModelAdapter`.
- **Clear extension points**: custom tools, plugins, 13 lifecycle hooks, model adapters and trace exporters all register through the public API, so you never patch the engine.
- **Dependable execution**: every tool call goes through the permission checks and its receipt is saved. Interrupted tasks resume explicitly; operations whose outcome is unknown are never blindly replayed.
- **Visible cost**: run, model and tool spans export to JSONL or OpenTelemetry (Langfuse and others). Requests are priced per model, and a task can have a spending cap. The cap stops new requests once it is reached; requests already in flight are still billed, so the final bill can exceed it.
- **Batteries included**: files, commands, search, browser screenshots, MCP, Skills, parallel subagents, long-context compaction and SSH remote sessions.

## Install

Python 3.10+ is required. Local code search uses [ripgrep](https://github.com/BurntSushi/ripgrep) (`rg`) on PATH.

Xuloom is not on PyPI yet; install it straight from GitHub:

```bash
pip install "xuloom @ git+https://github.com/Dheudb/xuloom.git"              # library and CLI
pip install "xuloom[mcp,otel] @ git+https://github.com/Dheudb/xuloom.git"    # optional: MCP client, OpenTelemetry export
pip install "xuloom[pdf] @ git+https://github.com/Dheudb/xuloom.git"         # optional: PDF reading (PyMuPDF, AGPL-3.0, ~200 MB)
```

Append a tag such as `@v0.1.45` to pin a version. To work on the source, clone it and install in editable mode:

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

**Custom tools**: a typed function with `@tool` is a tool. The input schema comes from the type hints and the description from the docstring; it goes through the same validation, approval and receipts as the built-in tools. Subclass `BaseAgenticTool` when you need finer control.

```python
from xuloom import tool

@tool(read_only=True)
def ticket_lookup(ticket_id: str) -> str:
    """Return the status of an internal ticket.

    Args:
        ticket_id: Ticket identifier such as "T-123".
    """
    return f"{ticket_id}: open"

config = config.replace(extra_tools=(ticket_lookup,))
```

**Plugins** bundle tools, hooks, model adapters, Skills, subagents, CLI commands, desktop RPC methods and trace exporters for reuse across projects. The built-in media generation feature is itself a plugin (`xuloom/media/plugin.py`).

```python
def register(api):
    api.add_tool(ticket_lookup)
    api.add_hook("BeforeToolUse", block_production_writes, tools=["bash_tool"])
    api.add_model_adapter(MyAdapter())
    api.add_trace_exporter(MyExporter())
```

| Extension point | Purpose | Docs |
| --- | --- | --- |
| `@tool` / `BaseAgenticTool` / `extra_tools` / `disabled_tools` | Add or remove the tools the model sees | [extending](docs/en/extending.md#tools) |
| Plugins (`register(api)`, entry points, `xuloom plugins new/enable`) | Package, enable and disable a set of extensions | [plugins](docs/plugins.md) |
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
xuloom plugins new my-tools                                   # plugin template with an offline test
xuloom plugins list
xuloom serve --root ./work                                    # HTTP + SSE API, see docs/server.md
```

`exec` exit codes: 0 completed, 3 waiting for approval or input, 8 budget or cost cap exhausted. The rest are listed in [CLI](docs/cli.md).

## Desktop workbench

The Windows x64 desktop app runs the same engine and ships its own Python runtime. Download `Xuloom-v<version>-windows-x64.zip` from [Releases](https://github.com/Dheudb/xuloom/releases), extract the whole archive and run `Xuloom.exe`. Then enter your API endpoint, key and model under **Settings → Model connections**. The interface is in Chinese by default; switch to English under the **Language** selector at the top of Settings. Each reply shows its tokens and cost; set model prices and a per-task cost cap under Settings → Cost (use 0 for local models). There is no desktop build for Linux or macOS yet, and no signed installer or auto-update; on those platforms use the library and the CLI.

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
