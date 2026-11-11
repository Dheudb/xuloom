# 项目结构

```text
xuloom/
  main.py             CLI 与终端交互入口
  core/               引擎循环、配置、消息、会话与生命周期钩子
    engine.py         QueryEngine：构造、连接切换、生命周期与公开入口
    engine_config.py  EngineConfig：全部构造参数与环境变量默认值
    engine_parts/     组成 QueryEngine 的职责模块（见下文）
  desktop/            桌面 JSON-lines RPC、审批和任务生命周期
  context/            提示词、附件、文件状态与工具结果
  memory/             上下文压缩、Token 估计和会话记忆
  tools/              文件、命令、规划、网页和浏览器等工具
  models/             ModelAdapter 接口、注册表与内置 openai/anthropic 适配器
  plugins/            插件加载、启用列表、PluginAPI 与内置插件清单
  media/              内置插件 media：生成工具、CLI 命令与桌面 RPC，全部经 PluginAPI 注册
  skills/             Skill 发现、校验、资源访问和加载
  agents/             子代理定义、执行、后台任务与团队协调
  protocols/          MCP 配置/市场/连接，以及 LSP 管理
  security/           审批、工作区路由、Windows LPAC 系统隔离与凭据保护
  features/           浏览器、规划及可选的领域能力与兼容状态
  utils/              Shell 任务、VCR 和结果压缩等辅助代码
desktop/
  electron/           窗口、IPC、引擎进程和运行时选择
  src/                React 工作台
  scripts/            开发服务、运行时准备和 Windows 打包
  tests/              界面状态与隐藏 Electron 端到端测试
tests/                Python 单元/集成测试和本地协议测试服务
examples/             可导入的 Skill 与代理示例
docs/                 使用与开发文档
```

## QueryEngine 的组成

`QueryEngine` 由 `xuloom/core/engine_parts/` 中的职责模块组合而成，每个模块只负责一类状态：

| 模块 | 职责 |
|---|---|
| `loop.py` | 回合循环。`_run_loop` 只做编排，阶段方法分别是 `_begin_run`（门禁、Skill、用户消息、恢复）、`_drain_inbound_messages`、`_build_turn_request`、`_run_tool_phase`、`_finish_turn` |
| `sampling.py` | 一次模型请求：原生流式或适配器响应、fallback 判断、VCR 回放事件、输出上限 |
| `usage.py` | token 用量、请求费用、输出预算和任务花费上限 |
| `recovery.py` | 回合失败的处理：`_handle_turn_exception`、`_recover_max_tokens`、`_repair_tool_protocol`、`_recover_context_overflow`、未完成工具回合的回滚 |
| `tools.py` | 工具注册、能力事件、并发批次、Hook 调用、子代理收尾 |
| `messages.py` | 与供应商无关的消息整形和历史清理 |
| `context.py` | 压缩边界、提示缓存、附件、记忆文件 |
| `persistence.py` | 会话加载、元数据、保存、父子状态传递 |
| `reasoning.py` | 思考/推理配置 |
| `modes.py` | 协作、Plan、Coordinator 模式和 Ultraplan |
| `auto_commit.py` | Git 自动提交提案与单文件撤销 |
| `status.py` | CLI/桌面使用的运行状态摘要 |

混入类共用的属性和方法签名声明在 `xuloom/core/engine_state.py` 的 `EngineState` 中（由 `scripts/gen_engine_state.py` 生成，运行时为空类），供 mypy 检查每个混入类；见[开发文档](development.md)的“类型检查覆盖范围”。

内置工具集合在 `xuloom/tools/builtin.py`（`default_tools()` 与 `LATENT_TOOL_NAMES`）。阶段方法以 `_` 开头，属于内部实现，可以随版本调整。

## 一次桌面任务

React → 受限 Electron IPC → 项目会话的引擎进程 → QueryEngine → 模型/工具循环。
引擎把流式文本、工具状态和审批请求以事件返回界面。审批会等待用户响应。
桌面没有直接解析终端输出，也没有向渲染进程开放 Node.js。

CLI 直接使用 QueryEngine。Windows 便携包通过内置 Python 的隔离模式启动引擎，
源码开发模式使用开发环境。各 MCP 连接有独立的异步生命周期，连接失败彼此隔离。

## 配置与产物

| 位置 | 数据 |
|---|---|
| `~/.xuloom/config.json` | 模型连接和用户配置 |
| `<用户数据>/Xuloom/history/<工作目录标识>/` | CLI/桌面共用的会话、索引、子代理和工具输出；Linux 目录名为 `xuloom` |
| `<用户数据>/Xuloom/session-locks/` | 跨进程写者锁；标记文件长期保留，占用随操作系统文件句柄释放 |
| `<项目>/.xuloom/capabilities.json` | 导入的 Skill/Subagent 配置目录 |
| `<项目>/mcp_servers.json` | 项目 MCP 服务与凭据 |
| `<用户数据>/Xuloom/workbench.json` | 桌面项目列表、会话关联与界面设置 |
| `desktop/dist/` | 前端编译结果 |
| `desktop/build/python/` | 打包用的内置 Python 与依赖 |
| `desktop/release/` | Windows 便携应用 |
| `desktop/test-results/` | 隐藏窗口验收截图与测试记录 |

模型配置、会话和构建产物均不进入源码版本控制。
