# Xuloom

**Scaling swarm workbench — experimental / development preview:** an opt-in, high-usage workspace for optional local execution and SSH nodes with per-host capacity, adaptive admission, model-planned exploration, durable task identities, evidence aggregation and explicit recovery. Workers are balanced into groups of at most 50 by default (51 becomes 25 + 26), each with an independent Xuloom coordinator session. Chat with the primary during a run; requested changes of direction wait for old work to stop, retaining history and artifacts. Desktop and `xuloom scaling` share the same controller. It does not change Solo/Auto/Ultra. See [configuration and validation boundaries](docs/scaling.md).

A local, general-purpose agent workbench with a desktop app and CLI sharing one Python engine. Connect your own model; use Skills for reusable workflows, Subagents for delegation, and MCP for external tools.

[中文](README.md) · [Windows downloads](https://github.com/Dheudb/xuloom/releases) · [Documentation](docs/README.md)

## Start here

Download the Windows x64 ZIP from Releases, extract the whole directory, and run `Xuloom.exe`. Keep its `resources` directory; GitHub's Source code archive is not the app. The portable package includes Python and the MCP SDK. You do not need to choose an interpreter or install Node.js for the app itself.

Open **Settings → Model connections**, enter your API endpoint, protocol, model ID and key. A connection can list multiple models; multiple providers are supported. OpenAI-compatible Chat Completions and Anthropic Messages are supported. Local model servers must expose a supported interface. Text completion alone does not establish compatibility with tools or images.

Choose **New conversation** to get an independent working directory, or open an existing folder. A Git repository is not required. Source version: **v0.1.23**; see Releases for currently published binaries and checksums. macOS/Linux desktop distributions are not available.

**Native guarded continuations:** Normal tool calls can declare literal follow-up action graphs. Before another model request, the engine checks real receipts and advances eligible actions, including branches, independent read-only fan-out and verified recovery. Each action retains native authorization, confinement and persistence. Unknown evidence, errors or new input return control to the actor; undeclared workflows keep their original loop. App and CLI share this path without discovering an extra tool. See [native continuations](docs/native-continuations.md); explicit [tool sequences](docs/tool-sequences.md) remain available.

SSH conversations can preview remote workspace HTML and its relative assets through the existing encrypted connection. There is no webpage port forwarding, local-path fallback or model request. `xuloom remote preview <connection> <HTML path> --json` queries the same authorized resource; optional bounded chunks support programmatic clients. Both endpoints need this protocol. See [remote HTML](docs/remote.md#网页预览与资源读取).

Execution traces use compact rows with icons for images, edits, terminal commands and searches. Expand a row for its inputs and output; completed turns collapse automatically. Image reads show clickable thumbnails backed by session snapshots, so previews survive changes or deletion of the original file. Previewing does not call a model; model image understanding requires an endpoint supporting image input.

For visual web checks, ask the agent to render workspace HTML or a local development URL. `browser_tool` with `action="render"` captures a hidden page and returns the screenshot directly to a vision-capable model. Local desktop uses its bundled Chromium; CLI and SSH need `pip install "xuloom[browser]"` and `python -m playwright install chromium`. For SSH, install those dependencies on the execution host; files and localhost stay remote, with no local renderer fallback. Existing approvals apply, and system execution isolation continues to block browser tools. Text-only models receive the saved path with an explicit limitation. See [background rendering](docs/browser-capture.md).

## Configure extensions in Settings

Open a conversation first; extension management does not require a model API call.

| Settings page | Actions |
| --- | --- |
| Skills | Create a skill, import an existing directory, refresh, inspect instructions, open configuration folders |
| Subagents | Create a role, set its model/tools/skills, import definitions, inspect instructions |
| MCP services | Open the existing MCP manager for marketplace browsing, custom configuration, testing and enable/disable |

New Skill/Subagent definitions can be global or local to the current workspace. They are real Markdown files, outside the EXE. Imported directories are mounted in place, not copied. Keep the original directory available. Existing files are never overwritten by the creation form.

## Skills

Place one skill in `<workspace>/.xuloom/skills/<name>/SKILL.md`, or `~/.xuloom/skills/<name>/SKILL.md` for all workspaces:

```markdown
---
name: literature-review
description: Compare primary research on a specified topic and cite sources.
---
Clarify the research scope. Find original sources, distinguish evidence from
inference, compare findings, and provide clickable citations.
```

The name must match the directory: lowercase letters, digits and single hyphens, at most 64 characters. Optional `references/`, `scripts/` and `assets/` live alongside `SKILL.md`.

Type `$` in the composer to select valid skills, then write your request. Skills are available by default and do not grant additional permissions. Public built-ins: `code-development`, `code-review`, `debug-issue`, `web-research`, `skill-creator`, `data-analysis`, `document-writing`, `document-summary`, `experiment-design`.

## Subagents

Place definitions in `<workspace>/.xuloom/agents/` or `~/.xuloom/agents/`. For example, `source-checker.md`:

```markdown
---
name: source-checker
description: Independently check evidence in supplied materials.
model: inherit
tools: Read, Grep, Glob, Skill
skills: [literature-review]
---
Check the assigned claims. Report supported, unsupported and contradictory
statements with sources. Do not modify files.
```

Install the referenced skill first. `model: inherit` uses the current model. By default another model ID uses the current connection; set `connection: <saved connection ID>` to select another configured provider. Omit `tools` to inherit available tools, or use `tools: []` in a manual definition to provide none. Tool aliases include Read, Write, Edit, Exec, Grep, Glob and Skill.

Optional [Claude Code / Pi / Codex / DSH backends](docs/external-agents.md) use your installed CLI, native worker lifecycle and authorized tools. Codex requires an OpenAI Responses endpoint; a Chat Completions-only gateway is insufficient. Successful Codex follow-up verifies the owned rollout and checksum; unknown interrupted work is never silently replayed. Discovered tool visibility survives session restoration within the current role's scope; cached MCP definitions do not grant execution rights. DSH uses a minimal owned headless profile (tested 0.2.1-rc.5), text task input and native MCP image results; see [its checks](docs/dsh-backend-validation-2026-11-02.md). See the [discovery and recovery checks](docs/tool-discovery-validation-2026-11-02.md). Full external CLI over SSH integration and native cross-platform releases remain part of the [ongoing scope](docs/product-roadmap.md).

Assign tasks through the Agents panel or name the role in your request. Creating a definition does not launch it. Workers have independent contexts but share workspace files and remain subject to permissions and budgets. Solo disables delegation; Auto delegates when useful; Ultra actively looks for useful parallel work. Auto and Ultra differ only in delegation initiative, with no default concurrency, total-worker or shared-output quota. Explicit host limits apply equally to both; accepted work queues automatically when a finite concurrency is configured.

## MCP

Go to **Settings → MCP services → Open MCP manager**. Choose a catalog entry or add a custom service. Local stdio services need a command, argument array and optional environment variables. Remote Streamable HTTP services need a URL and optional headers.

Save, test the connection, then explicitly enable the service. Installation saves a disabled configuration. Testing starts the program or accesses the remote service; it does not call the chat model.

Workspace configuration is `<workspace>/.xuloom/mcp_servers.json`:

```json
{
  "mcpServers": {
    "local-files": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/allowed-folder"],
      "env": {},
      "enabled": false
    }
  }
}
```

This example needs Node.js/npm and a real allowed directory. Remote services use `type: "http"`, `url`, and `headers`. Keep credentials in headers/environment rather than visible command arguments. Use trusted services. OAuth and legacy SSE are not implemented. Windows system execution isolation disables MCP.

## Data, permissions and limits

Model connections live in `~/.xuloom/config.json`; global extensions in `~/.xuloom/skills/` and `~/.xuloom/agents/`. Workspace configuration lives in `.xuloom/`; chat history is centralized outside workspaces. Windows data uses `%LOCALAPPDATA%/Xuloom`, Linux uses `$XDG_DATA_HOME/xuloom` or `~/.local/share/xuloom`, and macOS uses `~/Library/Application Support/Xuloom`.

Desktop and CLI share storage with a single-writer session lock, not live collaborative editing. Moving a conversation to a project changes its future working directory without moving its old files.

Ask for approval and Full access govern tool authorization. Neither is an OS sandbox. Optional Windows x64 LPAC execution isolation is available from the composer's plus menu; Git worktrees/copies only isolate file changes. See the documented restrictions before using it.

The plus menu also selects goal mode: write a goal as your next prompt, then manage the persistent strip. Model completion claims do not establish domain acceptance. Automatic updates, signed installers and macOS/Linux desktop packages remain unavailable. Real long-task and cross-platform validation remains ongoing.

Private skills, credentials and chat histories are not part of the public distribution. Model and external tool calls still send their supplied task content to the configured service.

## Source development and CLI

Recommended: Python 3.12+, Node.js 24+, Git and ripgrep (`rg`). Base CLI supports Python 3.9+; MCP needs Python 3.10+.

```bash
git clone https://github.com/Dheudb/xuloom.git
cd xuloom
python -m venv .venv
# Activate .venv, then:
python -m pip install -e ".[mcp]"
xuloom
# Non-interactive execution:
xuloom exec "Your task" --cwd /path/to/work --json
# Desktop development:
cd desktop
npm ci
npm run dev
```

See [models](docs/models.md), [Skills/Subagents](docs/capabilities.md), [MCP](docs/mcp.md), [CLI](docs/cli.md), [goals/scheduling](docs/goals-and-schedules.md), [execution isolation](docs/queue-and-isolation.md), and [development](docs/development.md). GitHub Actions runs are manual.

SSH connections are shared by the desktop and `xuloom remote chat CONNECTION` / `xuloom remote exec CONNECTION "task" --json`. The remote installation owns model credentials, history and execution. Local and SSH chats support deep links; offline navigation references can be removed without deleting remote history. See [remote execution](docs/remote.md) and [chat links](docs/chat-links.md).

Native SSH subagents share the existing worker FIFO, permission checks and receipts. In **Settings → Subagents**, choose an SSH execution location and a model connection on that host. Both hosts need a compatible v0.1.6 worker protocol. Follow-up uses the same remote history; messages and cancellation use the owned worker identity. Completed workers without background commands release their SSH connections. Their history opens read-only, with an independent fork available. Local model credentials and Skill files are not copied; explicitly inherited conversation text is reference material sent to the host. See [distributed workers](docs/distributed-workers-plan.md). Local Claude Code, Pi, Codex and DSH backends use installed CLIs; full external-over-SSH validation remains on the [roadmap](docs/product-roadmap.md).

Media generation supports configured fal Queue endpoints for images, image edits, video, speech, sound and music. Users supply their own keys and models. Media controls and artifacts are shared by the App and CLI; this does not imply compatibility with arbitrary chat APIs or verified real-provider generation. See [media configuration and limits](docs/media.md).

## License

[MIT](LICENSE)
