# Xuloom

面向开发者的开源 Agent harness：一个可以嵌入自己程序的 Python 引擎，负责模型调用、工具执行、权限、子代理、上下文压缩、会话持久化和中断恢复；同时附带一个使用同一引擎的桌面工作台和 CLI。

[English](README.en.md) · [文档导航](docs/README.md) · [当前能力表](docs/current-capabilities.md) · [更新日志](CHANGELOG.md) · [下载 Windows 桌面版](https://github.com/Dheudb/xuloom/releases)

- **接任意模型**：内置 OpenAI 兼容 Chat Completions 与 Anthropic Messages；其他 API 写一个 `ModelAdapter` 即可接入。
- **扩展点明确**：自定义工具、插件、13 个生命周期 Hook、模型适配器、追踪导出器，都通过公开 API 注册，不需要修改引擎。
- **可靠的执行**：每次工具调用先检查权限，再落盘回执；中断后显式恢复，不会猜测重放结果不确定的操作。
- **看得见成本**：run / model / tool span 可导出到 JSONL 或 OpenTelemetry（Langfuse 等），按模型价格计算花费，并可设置任务花费上限。
- **开箱即用的能力**：文件、命令、搜索、浏览器截图、MCP、Skills、子代理与并行协作、长上下文压缩、SSH 远程会话。

## 安装

需要 Python 3.10+。本地代码搜索使用 [ripgrep](https://github.com/BurntSushi/ripgrep)（`rg`），请先装好并加入 PATH。

Xuloom 目前还没有发布到 PyPI，请直接从 GitHub 安装：

```bash
pip install "xuloom @ git+https://github.com/Dheudb/xuloom.git"              # 核心库与 CLI
pip install "xuloom[mcp,otel] @ git+https://github.com/Dheudb/xuloom.git"    # 可选：MCP 客户端、OpenTelemetry 导出
pip install "xuloom[pdf] @ git+https://github.com/Dheudb/xuloom.git"         # 可选：读取 PDF（PyMuPDF，AGPL-3.0 许可，约 200 MB）
```

在命令末尾加 `@v0.1.45` 这样的标签可以固定版本。需要修改源码时，克隆后以可编辑方式安装：

```bash
git clone https://github.com/Dheudb/xuloom.git
cd xuloom
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[mcp]"
```

## 在程序中使用

```python
import asyncio
from xuloom import ApprovalPolicy, EngineConfig, QueryEngine

config = EngineConfig(
    model="gpt-4.1", model_provider="openai",          # 或 "anthropic"、已注册的适配器名
    api_base_url="https://api.openai.com/v1", api_key="sk-...",
    cwd="./work", approval_policy=ApprovalPolicy("full"),
)

async def main():
    async with QueryEngine(config) as engine:
        result = await engine.run("读取 README.md，用三句话总结")
        print(result.ok, result.reason, result.text)

asyncio.run(main())
```

`run()` 返回 `RunResult`：是否完成要看 `ok` / `reason`，不能只看文本。需要流式事件（文本增量、工具调用、token 与花费）时使用 `async for event in engine.submit_message(...)`。所有配置项、事件和恢复方式见 [框架接口](docs/framework.md) 与 [开发者扩展](docs/extensions.md)。

## 扩展

**自定义工具**：带类型注解的函数加上 `@tool` 就是一个工具。参数结构来自类型注解，说明来自文档字符串；它与内置工具一样经过参数校验、权限审批和回执记录。需要更细的控制时，也可以继承 `BaseAgenticTool`。

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

**插件**：把工具、Hook、模型适配器、Skills、子代理、CLI 命令、桌面 RPC 和追踪导出器打包，供多个项目复用。内置的媒体生成功能本身就是一个插件（`xuloom/media/plugin.py`）。

```python
def register(api):
    api.add_tool(ticket_lookup)
    api.add_hook("BeforeToolUse", block_production_writes, tools=["bash_tool"])
    api.add_model_adapter(MyAdapter())
    api.add_trace_exporter(MyExporter())
```

| 扩展点 | 用途 | 文档 |
| --- | --- | --- |
| `@tool` / `BaseAgenticTool` / `extra_tools` / `disabled_tools` | 增加或移除模型可用的工具 | [开发者扩展](docs/extensions.md) |
| 插件（`register(api)`、entry point、`xuloom plugins new/enable`） | 打包并启用、停用一组扩展 | [插件](docs/plugins.md) |
| 生命周期 Hook（13 个事件） | 拦截或改写工具调用、在任务前后注入上下文、审计 | [Hook](docs/hooks.md) |
| `ModelAdapter` | 接入任意模型 API | [模型适配器](docs/model-adapters.md) |
| 追踪导出器、价格、花费上限 | 可观测性与成本控制 | [追踪与花费](docs/observability.md) |
| `ScriptedModel`、`ReplayModel`、`xuloom eval` | 不用真实模型的测试、离线回放与批量评测 | [回放与评测](docs/replay-and-eval.md) |
| Skills（`SKILL.md`）、子代理定义（`.md`） | 无需写代码，复用流程和角色 | [Skills 与子代理](docs/capabilities.md) |
| MCP 服务 | 连接外部工具服务（stdio / Streamable HTTP，支持 OAuth） | [MCP](docs/mcp.md) |

完整示例：[examples/extension_project](examples/extension_project/)（自定义工具、事件、取消与恢复）和 [examples/plugins/ollama-native](examples/plugins/ollama-native/)（Ollama 适配器、工具、Hook 与 Skill）。

## CLI

```bash
xuloom                                                  # 交互式终端
xuloom exec "修复 tests 中失败的用例" --cwd ./repo --json   # 非交互执行：JSON 事件流，退出码表示结果
xuloom plugins new my-tools                             # 生成插件模板（含离线测试），见 docs/plugins.md
xuloom plugins list
xuloom serve --root ./work                              # HTTP + SSE 接口，见 docs/server.md
```

`exec` 的退出码：0 表示完成，3 表示等待审批或输入，8 表示预算或花费上限已用尽，其余见 [CLI](docs/cli.md)。目标、定时唤醒和条件跟进见 [目标与调度](docs/goals-and-schedules.md)。

## 内置能力

| 能力 | 说明 |
| --- | --- |
| 工具 | 文件读写与编辑、命令和后台任务、代码搜索、网页搜索与读取、浏览器渲染截图、图片输入、Todo 与计划。首轮只声明常用入口，其余按需发现，详见[首轮输入与原生搜索](docs/lean-context-search.md) |
| 权限 | “请求批准 / 完全访问”两档，显式拒绝规则，`BeforeToolUse` Hook，以及系统级[执行隔离](docs/queue-and-isolation.md)（Windows LPAC；Linux bubblewrap 与 macOS seatbelt 为实验后端） |
| 子代理与协作 | Solo / Auto / Ultra 三档协作模式、后台并行、汇合与流水线，角色可以使用不同的模型连接，见[协作组合](docs/coordination.md) |
| 上下文 | 分层压缩，原文归档，以及长任务记忆，见[上下文与记忆](docs/memory-and-compaction.md) |
| 会话 | 集中存储、单写者锁、检查点、显式恢复、运行中追加指令，见[运行中追加指令](docs/live-input.md) |
| 远程 | 通过 SSH 打开另一台机器上的会话，并支持远端子代理，见 [SSH](docs/remote.md) |
| 媒体生成（内置插件） | 图片、视频、语音、音效、音乐，见[媒体生成](docs/media.md) |

## 桌面工作台

Windows x64 桌面版使用同一个引擎，自带 Python 运行时。使用步骤：

1. 在 [Releases](https://github.com/Dheudb/xuloom/releases) 下载 `Xuloom-v版本号-windows-x64.zip`，完整解压后运行 `Xuloom.exe`。不要只复制 EXE。
2. 在 **设置 → 模型连接** 填写 API 地址、Key 和模型。
3. 新建会话后直接输入任务，每个会话都有独立的工作目录。

Skills、子代理、MCP、SSH 远程和生成模型都在设置中配置，见 [桌面 App](docs/desktop.md)。每轮回复下显示 token 和费用；在 **设置 → 费用** 填写模型价格和每个任务的花费上限（本地模型可填 0）。上限是停止条件：达到后不再发出新请求，但已发出的请求仍会计费，最终账单可能略超上限。界面支持中文和英文，在 **设置窗口顶部 → 界面语言 / Language** 切换。Linux/macOS 目前只能使用源码 CLI 和库，还没有桌面发行包，也没有签名安装器和自动更新。

| 内容 | 位置 |
| --- | --- |
| 模型连接与 Key | `~/.xuloom/config.json`（Windows 使用当前用户的原生加密，见 [凭据](docs/credentials.md)） |
| 插件、价格 | `~/.xuloom/plugins.json`、`~/.xuloom/pricing.json` |
| 全局 Skills / 子代理 | `~/.xuloom/skills/`、`~/.xuloom/agents/` |
| 工作区扩展与 MCP | `<工作目录>/.xuloom/` |
| 会话历史 | Windows `%LOCALAPPDATA%/Xuloom/`；Linux `$XDG_DATA_HOME/xuloom/`；macOS `~/Library/Application Support/Xuloom/` |

## 实验功能

以下功能可以使用，但接口和行为仍可能变化，验证范围以各自文档为准：

- **[Scaling](docs/scaling.md)**：在本机和多个 SSH 节点上运行分组 worker 的独立工作台。需要显式确认高用量，默认不启用。
- **[外部 Agent 后端](docs/external-agents.md)**：把 Claude Code、Pi、Codex、DSH 作为受控子代理使用。
- **[原生 SSH worker](docs/history/distributed-workers-plan.md)**：在已配置的 SSH 主机上运行子代理。

## 参与开发

```bash
pip install -e ".[dev,mcp,pdf]"
ruff check . && mypy && python -m unittest discover -s tests
```

代码布局和提交前检查见 [AGENTS.md](AGENTS.md) 与 [CONTRIBUTING.md](CONTRIBUTING.md)，构建和打包见 [开发](docs/development.md)。安全问题请按 [SECURITY.md](SECURITY.md) 私密报告。过去版本的验证记录在 [docs/history](docs/history/README.md)，只适用于记录中的版本和环境。

## 许可证

[MIT](LICENSE)
