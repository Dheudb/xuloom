# 可选外部 Agent：Claude Code / Pi

Xuloom 的默认后端仍是原生 QueryEngine。`backend: claude` 让已有角色通过用户安装的 Claude Code 执行任务，继续使用 Xuloom 的队列、身份、工具授权、结果记录与取消入口；不会下载或捆绑另一个 Agent、模型权重或供应商 SDK。当前实际验收版本为 Windows Claude Code `2.1.267`，本地 HTTP/SSE 与实际 CLI 联动；没有调用真实供应商或使用个人登录。

`backend: pi` 接入用户安装的 Pi，使用其正式 JSONL RPC 和内置 MCP，支持 **Anthropic Messages / OpenAI Chat Completions** 两种已配置连接。实际本机验收版本为 `@earendil-works/pi-coding-agent 1.4.0`、Node `24.14.0`。Pi 不读取自己的个人登录、项目扩展或私有模型设置，供应商请求继续由 Xuloom 的已有客户端发送。没有安装外部程序时明确失败，原生角色仍正常使用。

## 配置

桌面：设置 → Subagents → 新建，选择 Agent 后端“Claude Code”，选择 **Anthropic Messages** 协议的模型连接、已配置模型及允许工具。外部程序路径留空时在执行机器的 PATH 查找；也可以填写实际可执行文件的绝对路径。不会执行 `.cmd`、`.bat`、`.ps1` 或旧 `.js` 启动脚本；缺少受控接口的旧 CLI 明确报错。

CLI 使用同一角色文件，例如全局 `~/.xuloom/agents/external-reader.md`：

```yaml
---
name: external-reader
description: 独立核对所提供资料
backend: claude
connection: my-anthropic-connection
model: inherit
tools: [Read, Grep, Glob]
---
核对任务要求的证据，报告可确认的事实及不确定项。
```

`connection` 是用户在模型设置中建立的连接 ID，不填写 Key 本身；模型填写该接口的实际 ID，不使用 CLI 的 `sonnet` 等简称。可省略连接以继承主代理的 Anthropic 连接；不能将 OpenAI 协议 Key 隐式传给 Claude。程序路径可用 `backend_program: /absolute/path/to/claude` 明确指定。原有 `agent_tool`、`spawn_agent`、程序接入 `engine.agents()`、正式 CLI 和桌面角色选择沿用此定义，不新增独立任务调度器。

执行位置与 Agent 后端是两个维度。`executor: ssh:host-id` 仍引用已配置的 Xuloom 执行主机，在主机解析安装程序、连接、Skills 和 MCP；不上传本机个人配置或 Key。原生 SSH 回归通过，**外部 CLI 经 SSH 的完整联动仍待专项验证**，不能由本机 Claude 通过推出跨机已验证。

### Pi 角色

桌面同一表单选择 **Pi**，连接可选 Anthropic Messages 或 OpenAI Chat；CLI 读取同一 Markdown 定义：

```yaml
---
name: pi-reader
description: 用 Pi 独立核对资料
backend: pi
connection: my-openai-compatible-connection
model: inherit
tools: [Read, Grep, Glob]
---
核对任务提供的证据，区分事实与推断。
```

Pi 需由用户另外安装，要求 Node 至少 `22.19`。程序路径留空时解析执行机器 PATH 中的 Pi 安装位置；需要明确路径时，`backend_program` 填写已安装包的 `dist/bundle/cli.js` 绝对路径，不填写终端命令或 npm 启动脚本。Xuloom 验证包名、入口及程序/Node 哈希，直接启动 Node，不通过 Shell 执行 `.cmd`、`.ps1`。隐式 PATH 中来自任务工作区的程序不会被启动。安装包不携带 Pi、Node、其依赖或个人登录数据。

OpenAI Chat 使用模型真实 ID；“兼容”接口仍须遵守工具调用和流结束协议。这里只接入 Chat Completions，不假称支持 OpenAI Responses、Google 或所有供应商私有参数。用户已配置的 Thinking / effort 使用 Xuloom 原有参数适配器；明确关闭思考时不会再同时发送开启强度。模型输入能力已知时按声明限制，未知时保留原生透传，不在 CLI 内静默丢弃图片。

## 权限、身份与输出

- Claude 的原生执行工具关闭，用户/项目的 Claude 设置、插件和 Hook 不自动发现。只连接本任务拥有的本地 MCP 服务；安全的内部工具发现/结束控制不获得文件或命令权限。
- 每次 MCP 操作都进入 Xuloom 现有工具执行入口，保留 Plan、显式拒绝、父任务授权和副作用前检查点。CLI 的 MCP 自动允许只消除它自己那一层重复弹窗，不能覆盖 Xuloom 的授权。没有添加跳过全部权限参数。
- 外部 MCP 传输按序执行工具，防止并发写入绕过原生批处理的串行约束；原生角色之间的并行派发保持现有 Runtime 行为。
- 实际供应商 Key 保留在已有 Python 模型连接中。CLI 仅获得本任务临时回环令牌；临时连接文件在正常清理时删除。模型请求不得改变冻结的模型或暴露未受控工具。
- 真实工具调用 ID 由提供方响应直接关联到 MCP 调用，不让模型转抄。未知、重复模型 ID 或参数变化不自动执行；重复 MCP 传输复用已有持久化回执。
- 每次真实模型请求分别使用已有目标/委派预算。CLI 内部多轮不合并成一次免费请求；没有 usage 的不确定请求仍按已有保守规则结算。CLI 显示的目录价估算不表示实际账单。
- 程序哈希在入队时固定并在启动时复核。Claude 完成需要真实初始化身份、成功事件、正常进程退出及持久化；Pi 使用最终 `agent_settled`，再核对真实会话、模型、空队列/非流式状态与最终 assistant 成功原因。Pi 的较早 `agent_end` 不算结束；仅输出答案文字不算成功。
- OpenAI 工具参数分片先在回环边界核对，完整流结束后才授予对应真实调用 ID。缺失结束标志、重复身份、未受控工具、旧式未授权函数或供应商托管搜索不触发工具执行。没有 usage 的请求沿用原生保守结算，不当作零消耗。

## 按需工具发现

外部 CLI 缓存当前角色允许的 MCP 定义，实际模型请求只接收原生已激活工具。使用原有 `tool_search_tool` 发现并激活专业能力，下一轮即可使用；缓存定义不授予执行权限。Claude 的重复工具搜索层由框架关闭，用户无需再配置。

激活名称保存在现有中央历史的 `metadata.activated_tools`。恢复会话及子代理后按当前角色范围重新启用；晚到的 MCP 注册也经过同一范围检查。缺失工具不会触发任意导入或启动，禁止／只读／服务器约束仍然有效。旧历史没有该字段时从默认工具开始，按需要重新发现。详见 [工具发现与控制管道验证](tool-discovery-validation-2026-11-02.md)。

## 续聊、停止与边界

正常完成后的 `followup` 使用同一个真实外部会话 ID，由新进程恢复；运行中接收的父任务消息在结束前通过原生邮箱消费并保存。完整 JSONL、CLI 自己的会话及本地工具回执保存在中央历史的会话产物中，不放进工作目录 `.xuloom`。Pi 还核对历史文件、消息数量及所属路径，缺失历史不会偷偷建立空白同名会话。删除原会话会删除这些所属记录；分支不会复制外部执行权。

停止只作用于本次创建的进程、连接和已有原生工具所有权，不按进程名或保存的 PID 结束用户应用。Pi 的拥有者主动停止先关闭新请求与工具入口，清空队列、等待正式 abort/最终空闲，再对账原生工具回执、RPC 增量历史与实际 JSONL 文件；完整内容、文件指纹及保存均确认后，返回 `external_recovery.resumable: true`。主任务可明确 `followup` 同一 worker，由新进程打开已验证的原历史文件；这不是自动重放或从空白重新执行。App 显示同一恢复状态，正式 CLI 的 JSON 回执也包含该字段。

进程崩溃、停止确认被再次中断、结果未被外部历史完整接收、文件遭修改或保存失败，均不能变成可恢复成功；保留已有记录并拒绝自动重发。`external_recovery.resumable: false` 需先核对副作用。Claude 的异常中断仍采用此保守边界，不能从 Pi 的确认式停止推断其他后端也可恢复。相关本机实程序、故障及 App/CLI 检查见 [停止与恢复验证](external-recovery-validation-2026-11-02.md)。

当前系统隔离后端不支持此外部进程时，在执行前拒绝，不退回宿主运行。工具授权也不等于对可信第三方程序本身建立 OS 沙箱。Linux/macOS 原生 CLI、不同安装版本、外部 SSH、工作区移动后的恢复和长时间真实模型仍需专项验收；Codex/DSH 尚未接入，不假装共用一种协议。完整外部中间对话已保存原始 JSONL，但尚未全部映射进子代理历史界面。

实现依据为 [Claude Code CLI 官方参数](https://code.claude.com/docs/en/cli-reference)。可选本地实程序检查通过 `XULOOM_TEST_CLAUDE_PROGRAM` 指定已安装二进制，运行 `tests/test_external_agents.py`；请求只访问测试回环接口，既不是模拟 CLI，也不是一次真实供应商验收。

Pi 协议依据为其官方 [RPC](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md)、[CLI](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/cli.md)、[MCP](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/mcp.md) 与[模型设置](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/models.md)。`XULOOM_TEST_PI_PROGRAM` 指定已安装 `dist/bundle/cli.js` 后运行 `tests/test_pi_agents.py`；测试实际 Pi/Node 与本地协议服务，不消费供应商额度。
