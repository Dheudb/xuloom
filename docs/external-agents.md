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

## 续聊、停止与边界

正常完成后的 `followup` 使用同一个真实外部会话 ID，由新进程恢复；运行中接收的父任务消息在结束前通过原生邮箱消费并保存。完整 JSONL、CLI 自己的会话及本地工具回执保存在中央历史的会话产物中，不放进工作目录 `.xuloom`。Pi 还核对历史文件、消息数量及所属路径，缺失历史不会偷偷建立空白同名会话。删除原会话会删除这些所属记录；分支不会复制外部执行权。

停止只作用于本次创建的进程、连接和已有原生工具所有权，不按进程名或保存的 PID 结束用户应用。异常退出/强制停止后若无法可靠确认外部完成状态，返回 `external_outcome_unknown`，保留记录并拒绝自动重发。当前尚未实现中断任务的核对式恢复，需检查产物后新建任务；这与正常完成后的续聊不同。

当前系统隔离后端不支持此外部进程时，在执行前拒绝，不退回宿主运行。工具授权也不等于对可信第三方程序本身建立 OS 沙箱。Linux/macOS 原生 CLI、不同安装版本、外部 SSH、工作区移动后的恢复和长时间真实模型仍需专项验收；Codex/DSH 尚未接入，不假装共用一种协议。完整外部中间对话已保存原始 JSONL，但尚未全部映射进子代理历史界面。

实现依据为 [Claude Code CLI 官方参数](https://code.claude.com/docs/en/cli-reference)。可选本地实程序检查通过 `XULOOM_TEST_CLAUDE_PROGRAM` 指定已安装二进制，运行 `tests/test_external_agents.py`；请求只访问测试回环接口，既不是模拟 CLI，也不是一次真实供应商验收。

Pi 协议依据为其官方 [RPC](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md)、[CLI](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/cli.md)、[MCP](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/mcp.md) 与[模型设置](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/models.md)。`XULOOM_TEST_PI_PROGRAM` 指定已安装 `dist/bundle/cli.js` 后运行 `tests/test_pi_agents.py`；测试实际 Pi/Node 与本地协议服务，不消费供应商额度。
