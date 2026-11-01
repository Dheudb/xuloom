# 通用执行接口

CLI 与桌面端使用同一套执行引擎。下列能力通过模型工具调用提供，不需要额外启动服务。

## 程序调用（当前开发源码）

`run(text, selected_skills=[...])` 返回 `RunResult`；`submit_message` 的最终事件包含同一回执。使用 `async with QueryEngine(...) as engine`，或在 `finally` 中 `await engine.aclose()`，释放连接、所属任务和会话写者锁。

`QueryEngine` 可显式指定 `api_key` 和 `network_mode`，连接参数绑定实例及压缩器，不通过热切换改写进程环境。并发、恢复及鉴权边界见 [实例连接绑定](runtime-connections.md)。

`await engine.agents(action, **parameters)` 沿用 `agent_tool` 的动作与字段，返回 Python 字典。上层无需从自然语言提取 ID，且所有调用仍经过原权限入口和持久化执行日志。返回字段如下：

| 字段 | 语义 |
| --- | --- |
| `api_version` | 当前为 `1`；新增可选字段向后兼容，改变字段语义需升版本 |
| `session_id` | 框架真实会话 ID |
| `tool_use_id` | 本次授权与持久化执行记录的调用 ID |
| `ok` | 协调操作成功并已保存；不表示所有子任务成功或领域验收通过 |
| `data` | 原生工具生成的完整结构化结果，超过展示阈值也不会只返回截断预览 |
| `error` | 失败时的 `code` 和 `message`，此时 `data=null` |

```python
# request_id 应由上层在首次发送前持久保存，断线后复用。
launch = await engine.agents("spawn", prompt="列出给定材料中的证据缺口",
    count=2, run_in_background=True, request_id="saved-request-42")
if launch["ok"]:
    ids = [row["agent_id"] for row in launch["data"]["agents"]]
    while True:
        state = await engine.agents("wait", agent_ids=ids, timeout_ms=30000)
        if not state["ok"] or all(row["status"] not in {"queued", "running"} for row in state["data"]["agents"]):
            break
```

调用方需要配置审批处理器或明确选择审批策略；此示例不自动授予权限。检查 worker 的 `status` 和 `receipt` 判断真实执行结果。

Auto / Ultra 默认不设并发上限；只有显式配置有限的宿主并发数时，超出空闲槽位的已接纳任务才自动 FIFO 排队，不需要模型为并发容量拆批。单次 `spawn` 仍最多提交 10 个任务，这是传输批次大小，不是会话容量或累计任务配额。

`capacity` 返回的容量字段：

| 字段 | 语义 |
| --- | --- |
| `limit` | 当前宿主并发上限；`null` 表示未设置，Solo 为 `0` |
| `active` | 当前占用执行槽位的任务数 |
| `available` | 有限并发下剩余槽位；`null` 表示未设置并发上限，不是零容量 |
| `queued` | 当前等待执行槽位的任务数 |
| `auto_queue` | 当前为 `true`，已接纳任务由 Runtime 自动排队 |
| `queue_limit` | 宿主队列上限，默认 `null`；未设置时没有默认 1000 项限制 |
| `max_batch_size` | 单次 `spawn` 的批次上限，当前为 `10` |

`executing=false` 也可能表示尚在排队，不能据此当作完成。重启后 `status=queued`、`phase=awaiting_resume` 的任务通过 `resume_queue(agent_ids=[...])` 明确恢复；曾经运行的 `interrupted` 任务不自动重放。详见 [排队与执行隔离](queue-and-isolation.md)。

`spawn` 的可选 `request_id` 支持 1–128 个字母、数字、`_`、`-`。相同参数重复提交返回原记录与 `reused=true`，重启恢复后也不会再启动。相同 ID 修改参数会拒绝。注册中断只保存部分批次时明确报错，调用方应检查实际记录，不能盲目重新发起。省略 ID 的独立调用每次都是新任务。

`list/result/wait/capacity` 读取原生生命周期，`cancel` 停止所属工作，`followup` 明确继续同一 worker。重复读取结果不会重新执行。重启后的历史 `running` 记录显示为 `interrupted`；它不是当前存活证明，不会自动重跑。`wait` 的结构化模式等待任一目标结束即返回，要汇总全部结果需检查所有目标的 `status`，不能把 `queued` 或 `running` 当作结束。`pending` 表示当前进程是否仍有该任务的未结束执行或排队任务，`executing` 只表示当前是否占用执行槽位；两者都不能代替结果验收。遇到 `awaiting_resume` 或 `interrupted` 时应按恢复规则处理，不能盲目循环等待。

事件复用 `engine.tool_context["capability_event"]` 回调：持久化注册后通知启动/状态，保存结果与完成记录后通知完成。回调应快速返回且不抛异常。断线可能丢通知，上层应通过 `list/result` 恢复实际记录，不能仅因缺少通知重新启动任务。

`session_busy` 表示另一个写者占用，`session_conflict` 表示快照已过期；此时没有执行模型或工具。`tool_error` 表示参数、权限或工具操作失败；`persistence_error` 表示无法确认完整存储，可能已有副作用，需要读取实际记录后处理。框架没有为此增加另一套 Job 状态，也不承诺外部系统的 exactly-once 事务。

## 工具发现

### 实例工作目录与路径

`QueryEngine(cwd=...)` 在构造时保存绝对工作目录。项目指令、权限配置、Hooks、记忆、Git 状态与工作区副本使用这个实例目录，不要求调用方改变进程全局 `cwd`。一个进程中的多个实例可分别使用各自的目录。

文件读写、Notebook、目录搜索、LSP、本地浏览器路径及命令目录先按会话解析，再送到统一权限入口；权限请求里的绝对路径就是实际执行目标。工作区副本使用路径边界判断，不会把 `project-other` 误认为 `project` 的子目录。副本依然只是文件改动隔离，不是操作系统沙箱。

自定义 `BaseAgenticTool` 只有明确声明的 `workspace_paths` 才参与路径解析。例如 `workspace_paths = {"file_path": False, "cwd": True}`：`False` 解析已提供的路径，`True` 还将省略的目录设为会话目录。MCP 或其他远端工具参数中的 `path` 不会仅因名称相同而被改写成本机路径。上层应通过引擎的授权入口调用工具；直接调用工具的 `execute` 不提供权限与执行日志保证。

项目指令由 `ContextBuilder` 单一加载。默认读取 Xuloom 指令与 `AGENTS.md`；旧 `CLAUDE.md` 只在 `legacy_instructions=true` 时读取，不再通过记忆加载器隐式重复注入。自动记忆继续按项目目录隔离，作为历史上下文与证据，不授予额外权限。

目标模式、正式非交互 CLI 和定时消息使用同一引擎与会话，见 [目标与调度](goals-and-schedules.md)。

文件、命令、搜索、网页读取、Skill 和原生委派默认可用。浏览器、Notebook、REPL、LSP、工作区隔离、旧后台任务及团队工具通过 `tool_search_tool` 搜索并激活。激活只改变可见性，不改变权限、Plan 限制或中断恢复确认。

显式列出专用工具的自定义角色可直接加载相应工具，不要求额外允许发现工具。角色限制同时作用于默认和潜在工具，包括只读与 MCP 服务范围，不能通过发现重新引入被排除的能力。

命令工具的 `wait_ms` 单次等待范围为 0–120000 毫秒（最多 120 秒），程序完成时提前返回。它与控制进程总运行时长的 `timeout_ms` 分开设置，等待期间仍可停止任务。

## 工具调用恢复

DSML 中单独用 `arguments` 包裹完整 JSON 参数对象的兼容格式会直接解包；重复字段、混合包裹与普通参数、未知字段仍拒绝执行，解包后继续使用原工具的参数校验和权限检查。

DSML 调用格式错误会反馈具体工具和参数问题，请模型修正后继续；原生调用中的无效 JSON 不会被当成空参数执行。参数校验错误也在执行前反馈。连续失败最多修正两次，单次用户任务合计最多六次修正，之后明确中断并保留已完成的文件、调用结果和子代理记录。主代理与子代理使用同一恢复逻辑，不自动重放成功的操作。

桌面端折叠过程记录保留修正状态。被拒绝的 DSML 原始输出保存在集中历史的 `tool-results/<会话 ID>/protocol-errors/`，用于本地诊断。工具定义保留嵌套参数的完整 JSON Schema；Skill 指令和引用资源不会再被单条工具输出的 5000 字符预览替代，整体上下文预算仍生效。

## 子代理

`agent_tool` 支持：

| action | 行为 |
| --- | --- |
| `spawn` | 新建独立代理；`count=1..10` 对同一任务批量并行；不同任务可同轮多次调用 |
| `capacity` | 返回宿主的实际并发上限、执行与排队数；`limit/available=null` 表示未设置并发上限 |
| `list` / `result` | 返回 JSON 状态；区分 `pending`、`executing` 和已保存的 `status`，排队不等于完成 |
| `send` | 将 `prompt` 排入指定代理的邮箱，在下一次模型调用前读取；不会自动唤醒空闲代理 |
| `followup` | 按 `agent_id` 继续原代理，保留消息历史、角色指令和模型；运行中需先等待或使用 `send` |
| `wait` | 等待指定 `agent_ids` 中任意一个结束，最长 60 秒；超时保留正在执行的任务 |
| `cancel` | 取消任务及其命令会话，保留历史供后续继续 |
| `close` | 释放空闲代理及其命令会话，保留历史；关闭后不能继续该代理 |

`spawn` 默认保留文本回执；自动调度应指定 `response_format="json"`。
模型调用 `agent_tool` 时省略 `run_in_background` 默认后台启动，与 `spawn_agent` 一致；显式传 `false` 才在当前调用中等待。版本化程序接口 `engine.agents("spawn", ...)` 及直接 `AgentTool.execute` 保留原有前台默认，调用方可显式传 `true` 获取后台回执。子代理类型由当前有效角色生成枚举，未知角色在权限审批和执行前拒绝，不创建 worker。
`run_in_background` 允许主代理在子任务执行期间继续编排；最终回答会等待本会话运行中的子代理全部结束，并收集生成回答期间刚返回的结果再汇总。失败、取消或没有文本输出的任务不能算作成功；等待期间仍可停止本轮。
`wait` 的结构化模式使用 `response_format="json"`，默认等待 30 秒。
旧文本 `wait` 等待所选批次全部完成，同样有超时上限。
状态中的 `running_commands` 与 `pending_message_count` 单独计数，不等同于模型正在执行。

Auto / Ultra 默认不设并发数、累计子任务数或共享输出额度；两档只改变委派倾向。Solo 禁止委派，不会因“完全访问”而解除。前台与后台任务共用同一宿主容量。

如需限制本机或接口负载，在工作目录 `.xuloom/capabilities.json` 中配置独立的宿主限制：

```json
{
  "agent_limits": {
    "concurrency": 24,
    "max_delegations": null,
    "output_tokens": null,
    "request_output_tokens": 64000
  }
}
```

前三项可为正整数或 `null`；省略时默认不限制，不存在固定的 80 上限。`request_output_tokens` 必须为正整数，默认 64,000；它是每次响应的输出上限，不是任务数量。旧字段 `agent_concurrency` 仍可设置并发数，但 `agent_limits.concurrency` 优先；旧 `collaboration_profiles` 不再参与数量限制计算。完整规则见 [协作档位](capabilities.md#协作档位)。

例如并发数为 3 时，一批 10 个有效任务会被接纳，按最多 3 个执行、其余 FIFO 等待，结束一个补上一个；并发占满本身不会导致整批拒绝。显式累计配额或共享输出额度耗尽、底层宿主显式队列上限已满、Solo、参数或权限等错误仍可能拒绝请求；不能把“自动排队”理解为无条件接纳所有调用。`queue_limit` 不是 `agent_limits` 的配置字段，宿主需要时通过现有 `SubagentTasks` 设置。

配置重新加载且没有运行或排队中的代理后，新并发数才会应用，不热改已有任务。未设置框架上限不代表机器或供应商提供无限资源；实际请求仍可能因供应商限流或资源不足失败。

历史位于用户级集中存储。重启后曾经运行的未完成任务显示 `interrupted`，不会自动重跑；核查副作用后可明确 `followup` 同一代理。未启动的排队任务使用上文的 `resume_queue` 恢复。
旧版代理缺少完整角色快照时会明确拒绝恢复，而非用默认角色替代。
更换供应商或 API 地址后，需要恢复原连接才能继续原代理；API Key 不写入角色快照。

## 自定义角色

外部 Skill 可将自己的角色放在技能目录的 `agents/*.md`，并在该技能根目录提供 `mcp_servers.json`。
加载有效、未被同名技能覆盖的 Skill 时，会发现这些角色与 MCP 配置；它们保留在外部目录，不复制进应用或源码。
MCP 的 `command` / `args` 支持 `${python}`（当前 Xuloom 解释器）和 `${skill}`（此技能的绝对目录）。
工作区的 MCP 配置优先于技能默认配置；可在 MCP 管理中禁用技能提供的服务。
角色仍遵循下述工具与权限限制，不会因随 Skill 挂载而获得额外权限。

继续使用 `.xuloom/agents/*.md`（也支持 `.agents/agents`、`.claude/agents` 及导入目录）：

```markdown
---
name: evidence-reader
description: 查找资料并给出来源
model: inherit
tools: Read, Grep, Glob, Skill, web_search_tool, web_fetch_tool
web_search: enabled
mcp_servers: []
read_only: true
---
围绕分配的问题检索和阅读资料，区分证据与推断。
```

`web_search: disabled` 排除搜索、抓取和浏览器工具；`enabled` 只允许继承宿主已有能力，不会安装新工具。
省略 `mcp_servers` 继承宿主可用 MCP；`[]` 禁用 MCP；字符串列表只保留指定服务。
`read_only: true` 只保留声明为只读的工具，命令工具也会被排除。这是工具可用性约束，不是操作系统沙箱。
角色工具和潜在工具一起过滤，不能通过搜索工具重新激活被排除的工具。默认子代理不再嵌套委派。
不支持的 `sandbox_mode` 会报错，不会假装已有相应隔离。

子代理默认继承父任务的模型、供应商和实际思考选择。模型接口可声明 Thinking 开关和支持的 effort；
`reasoning_effort` 可显式请求 `none/minimal/low/medium/high/xhigh/max`，但只能使用目标模型允许的值。
Anthropic 模型声明相应能力后通过 `output_config.effort` 传递；不同模型不盲目继承父任务参数。
模型可用 `spawn_agent(prompt="独立任务")` 简化后台派发，仍使用 `agent_tool` 的原生命周期与权限。
详见 [模型思考控制](model-reasoning-2026-10-30.md)。

## 命令会话

新增 `command_tool`，保留已有 `bash_tool` 和后台任务接口。

- `start`：优先使用 `argv`（程序和参数数组），也可使用 `command` 执行 shell；默认工作目录为当前会话目录。
- `stdin`：直接传入 UTF-8 内容，不需要 shell 引号、临时文件或 Base64；默认发送后关闭输入。
- `keep_stdin_open=true`：保留管道输入，后续 `write` 发送文本，`eof=true` 关闭输入。
- `poll`：单次最长等待 120 秒，不因等待超时终止命令；执行总期限 `timeout_ms` 默认 120 秒、最大 600 秒。
- `read`：通过 `command_id`、`stream=stdout/stderr` 和 `offset` 分页，下一页使用返回的 `next_offset` 字节游标。
- `cancel`：停止该命令及其进程组/进程树；`list` 只展示创建代理拥有的命令。
- `capabilities`：查询实际能力；当前支持交互管道，不支持依赖真实 TTY 的终端程序。

标准输出和错误输出分开保存到集中存储中的会话工具资源目录。分页不会静默删除中间内容，
UTF-8 多字节字符跨页保留；只有读到 `eof=true` 且命令成功结束（`completed`、退出码 0），才能把整段控制 JSON 当作成功响应。
每个输出流上限 50 MiB，超限明确返回 `output_limit` 并终止进程。
命令 ID 限创建它的代理使用；同一代理继续任务时可继续访问。
正常完成子代理模型回合不会自动停止其独立命令，但取消/关闭代理或停止会话会清理它们。
重启后保留输出，旧进程显示 `interrupted`，不会按历史 PID 重新连接或终止进程。 外部子代理的停止还需检查 `external_recovery.resumable`：Pi 仅在正式停止、工具与持久化历史全部确认后允许明确续聊；未知结果不自动重放。详见 [外部后端边界](external-agents.md#续聊停止与边界)。

启动、写输入和取消都遵循现有批准权限，Plan 阶段不能借此执行命令。

## MCP

现有 `mcp_servers.json` 每项服务可设置 `tool_timeout_sec`（1–600，默认 20 秒）。
界面编辑其他字段会保留已设置的超时。支持含连字符的合法环境变量名称，凭据仍按原规则隐藏。

## 验证

技能可在 SKILL.md 的 YAML 中声明 `metadata.xuloom.completion-protocol: workflow-json-v1`。
只有本回合明确选择或通过 `skill_tool read` 加载的技能才启用完成检查。
其命令运行时可输出完整 JSON，以 `run_handle`（也可位于 `artifact_locator`）标识流程，
以布尔 `workflow_finished` 表示完成状态；带 `run_handle` 和 `next_command` 的初始化响应也表示未完成。
同一流程返回 `workflow_finished: true` 才清除未完成状态；明确受阻、失败或取消时可返回
`workflow_status: blocked/failed/cancelled`，并提供原因。检查只读取成功结束命令的完整 stdout，
不从普通文本、分页片段、非零退出码或其他代理输出推断完成状态，不自动执行返回的命令。
命令结果即使分页或进入溢出文件，检查仍使用最多 1 MB 的完整原始输出。
模型只描述下一步却结束时，最多连续提醒两次、每用户回合最多六次；仍未执行则显示中断，保留已有结果。
停止操作优先，新用户回合重新启用选定协议，不强行继续上一回合。普通聊天和未声明协议的技能不受影响。

`tests/test_agent_lifecycle.py` 与 `tests/test_command_sessions.py` 覆盖生命周期、权限和命令输入输出。
`npm run test:framework` 通过隔离的模型接口驱动真实桌面引擎，验证跨回合/重启继续、消息投递、
模型与推理参数、并发查询、UTF-8 输入以及命令历史输出。
