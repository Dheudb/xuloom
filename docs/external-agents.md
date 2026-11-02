# 可选外部 Agent：Claude Code / Pi / Codex / DSH

Xuloom 的默认后端仍是原生 QueryEngine。`backend: claude` 让已有角色通过用户安装的 Claude Code 执行任务，继续使用 Xuloom 的队列、身份、工具授权、结果记录与取消入口；不会下载或捆绑另一个 Agent、模型权重或供应商 SDK。当前实际验收版本为 Windows Claude Code `2.1.267`，本地 HTTP/SSE 与实际 CLI 联动；没有调用真实供应商或使用个人登录。

`backend: pi` 接入用户安装的 Pi，使用其正式 JSONL RPC 和内置 MCP，支持 **Anthropic Messages / OpenAI Chat Completions** 两种已配置连接。实际本机验收版本为 `@earendil-works/pi-coding-agent 1.4.0`、Node `24.14.0`。Pi 不读取自己的个人登录、项目扩展或私有模型设置，供应商请求继续由 Xuloom 的已有客户端发送。没有安装外部程序时明确失败，原生角色仍正常使用。

`backend: codex` 使用已安装 Codex 的正式 App Server JSONL 和 **OpenAI Responses**。它可以绑定与主代理不同的模型连接，复用原生队列、权限、预算、消息和中央历史；不是把另一套 Codex 登录或 API Key 交给子进程。当前本机协议验收版本是 Codex `0.156.1`，实际程序配合隔离回环 SSE/MCP 服务运行，未调用真实供应商。连接必须实际提供 `/responses`：只支持 Chat Completions 的“兼容 OpenAI”网关不能因此使用此后端。

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

Codex 角色使用同一配置入口，后端选择 Codex，模型连接选择支持 Responses 的 OpenAI 连接。例如：

```yaml
---
name: independent-reader
description: 独立核对提供的资料
backend: codex
connection: my-responses-connection
model: inherit
tools: [Read, Grep, Glob]
---
核对事实与证据来源，报告结果和不确定项。
```

Windows 的官方 npm 安装会解析已安装包的实际二进制，仍不经 Shell 执行 `.cmd`/`.ps1`。其他安装布局可填写 `backend_program` 的绝对可执行路径；缺少程序或接口不匹配时明确报错，不自动下载安装，也不回退到原生模型。Codex 的推理设置使用 Responses `reasoning.effort`；声明为其他供应商 thinking 格式的连接在启动前拒绝，不能把参数拼接成“兼容”。

Codex 的独立 `CODEX_HOME` 位于该 worker 的中央产物目录。App Server 的线程 ID、已完成回合、原始事件、诊断输出和 rollout 文件均保留；供应商 Key 只留在 Xuloom 的现有客户端中，子进程仅收到本次有效的回环令牌。部分 Codex 原生工具无法靠功能开关从目录中完全移除，因此中继同时过滤工具目录并在完整 Responses 回执确认后校验整批调用；所有获准调用仍经原生 MCP 授权，不能用 Codex 的 `approval_mode=auto` 覆盖父任务拒绝。未确认完成、非法调用或保存失败均不释放工具调用成功。

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

### DSH 角色

同一表单选择 **DSH · 已安装 CLI**，或在上述角色文件填写 `backend: dsh`。使用 **Anthropic Messages / OpenAI Chat Completions** 连接；不是 Responses 后端。当前核对的实际程序是 `@deepseek-ai/dsh 0.2.1-rc.5`、同版本 headless 包及 Node `24.14.0`。路径可留空在 PATH 查找，或填写已安装包 `lib/bin.js` 的绝对路径。未知协议版本在启动前拒绝，不自动下载 CLI，不使用个人 DSH 登录。

Windows 文件存储支持长路径；进程的当前目录另受系统限制。DSH 所属执行目录达到 260 字符时，启动前明确报错，不发起模型请求；可将 `XULOOM_DATA_DIR` 设置为较短的绝对目录。该限制不等于历史文件不能超过 260 字符：原生历史的长目录仍用完整内容核对、保存及清理。

DSH 每次调用使用自己的正式 headless 驱动与原生 Session。Xuloom 只组合所需核心、压缩、存储和 MCP 服务，不加载 DSH 的整个 base preset：文件、命令、子代理、Skill、插件管理、凭据和遥测插件均不自动启用。完整系统指令通过 DSH 的字面量 section 传入，保留 `{{...}}` 原文，不追加默认编程身份或额外规划请求。

执行主机的模型 Key 留在 Xuloom 客户端。DSH 只获得本次有效的回环令牌，并在所属中央产物目录启动；因此不会由启动器读取项目 `.env`。MCP 的文件和命令仍在角色的实际工作目录执行。其独立配置、v4 原生 JSONL、完整输出、诊断和工具回执保存在同一个 worker 的中央历史中；正常跟进须核对真实 Session、历史位置、完整文件哈希及已确认前缀。

DSH 已发布版的 JSON 过程事件有长度限制，不能直接作为完整结果。完成必须同时满足匹配的 session/turn 事件、`completed` 原生结束原因、正常进程退出、落盘历史与最终文字一致，以及全部原生工具回执对账。历史校验流式读取，不额外累积整份历史的工具正文。保存失败、外部工具调用变化或历史篡改不能显示 completed；无法确认的中断不自动重发任务。

当前 DSH headless 的初始任务接口只接受文本；直接传入图片会在模型请求前明确拒绝，不能丢掉图片后继续。工具返回的图片可进入其原生模型上下文：受控 MCP 在发送前显式转换为 8-bit RGB/RGBA PNG，保留像素尺寸，随后核对实际不可变图像对象与回执哈希；原工具产物仍沿用 Xuloom 存档。单张最多 20 MB、4000 万像素，任一边最多 16384；无损转换超出限制会报错，不悄悄缩小后冒充原图。见 [DSH 专项验收](dsh-backend-validation-2026-11-02.md)。

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

进程崩溃、停止确认被再次中断、结果未被外部历史完整接收、文件遭修改或保存失败，均不能变成可恢复成功；保留已有记录并拒绝自动重发。`external_recovery.resumable: false` 需先核对副作用。Claude 和 Codex 的异常中断仍采用此保守边界，不能从 Pi 的确认式停止推断其他后端也可恢复。Codex 正常完成后的续聊会校验实际 rollout 文件所属位置和 SHA256；缺失、变更或另一条线程不能变成空白恢复。相关本机实程序、故障及 App/CLI 检查见 [停止与恢复验证](external-recovery-validation-2026-11-02.md)和 [Codex 验证](codex-backend-validation-2026-11-02.md)。

当前系统隔离后端不支持此外部进程时，在执行前拒绝，不退回宿主运行。工具授权也不等于对可信第三方程序本身建立 OS 沙箱。四个后端经加密回环 SSH 的基础检查及成品控制端恢复见[专项记录](external-ssh-validation-2026-11-02.md)。Linux/macOS 原生、不同安装版本、其余外部 SSH 故障组合、工作区移动后的恢复和长时间真实模型仍需专项验收。DSH 与 Claude／Codex 的未知中断也不自动重放，不能从 Pi 的确认式停止推断其他后端都可恢复。完整外部中间对话已保存原始 JSONL，但尚未全部映射进子代理历史界面。

实现依据为 [Claude Code CLI 官方参数](https://code.claude.com/docs/en/cli-reference)。可选本地实程序检查通过 `XULOOM_TEST_CLAUDE_PROGRAM` 指定已安装二进制，运行 `tests/test_external_agents.py`；请求只访问测试回环接口，既不是模拟 CLI，也不是一次真实供应商验收。

Pi 协议依据为其官方 [RPC](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md)、[CLI](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/cli.md)、[MCP](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/mcp.md) 与[模型设置](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/models.md)。`XULOOM_TEST_PI_PROGRAM` 指定已安装 `dist/bundle/cli.js` 后运行 `tests/test_pi_agents.py`；测试实际 Pi/Node 与本地协议服务，不消费供应商额度。

Codex 依据 [官方 App Server](https://learn.chatgpt.com/docs/app-server)、[配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)及当前安装程序生成的协议 schema，Responses 依据[官方接口结构](https://developers.openai.com/api/reference/resources/responses/methods/retrieve)。`XULOOM_TEST_CODEX_PROGRAM` 指定实际可执行文件后运行 `tests/test_codex_responses.py`；桌面与正式 CLI 联动检查为 `npm run test:codex-workbench`。这些检查不使用个人 Codex 登录，也不证明其他安装版本或真实模型长任务已经验证。

## 只读查看完整执行历史

本机及 SSH 会话任务卡片中的“查看完整对话历史”从各后端的原生 JSONL 展示输入、回答、工具和公开推理；图片按需读取，长文本分页。CLI 和程序接口见[历史查看](agent-history.md)。查看不等于恢复执行，不改变现有写者的保存版本，也不替代原有回执验证。
