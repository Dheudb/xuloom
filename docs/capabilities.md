# Skills 与子代理

## 协作档位

输入框的协作菜单与执行模式、访问权限、模型思考强度分别设置。每个会话单独保存档位，默认 **Auto**。

| 档位 | 委派原则 | 数量限制 |
| --- | --- | --- |
| Solo · 独立 | 主代理自己完成，不启动或续接子代理 | 禁止委派 |
| Auto · 自动 | 工作独立且收益超过协调成本时委派 | 默认不设并发和累计任务上限 |
| Ultra · 增强 | 对可拆分的实质任务主动尽早派发独立调查、不同解法或验证，主代理同时推进主线 | 与 Auto 相同 |

Auto 和 Ultra 只改变委派倾向，不改变权限、模型思考强度或数量限制；不是定时器，也不强制固定的调用频率。不会为凑数量启动代理。启动、排队、等待、取消和恢复继续使用现有原生生命周期，第一版不增加递归派发。

Ultra 明确允许在有收益的独立工作中主动委派，无需用户每次另说“启动子代理”。简单问答和严格串行任务仍可直接完成。实际是否派发取决于模型对指令与任务的判断，不能把选中 Ultra 当成固定的调用概率保证。

默认不设累计子任务数、并发数或共享输出额度。每次模型响应仍有 64,000 tokens 上限，并受模型接口自身约束。单次批量调用最多提交 10 个任务，可再次提交；这个传输批次大小不是会话总量或档位配额。

需要控制本机/API 负载时，在工作区 `.xuloom/capabilities.json` 显式设置独立的宿主限制，两档使用同一份配置：

```json
{
  "agent_limits": {
    "concurrency": 10,
    "max_delegations": null,
    "output_tokens": null,
    "request_output_tokens": 64000
  }
}
```

`null` 表示不设置该项限制；未填写前三项也不限制。有限并发下，已接受的任务由 Runtime FIFO 排队，不要求模型手动拆批。累计用量保留用于查询，数量不作为任务完成标准。单次响应上限须为正整数。

旧的 `agent_concurrency` 作为独立宿主设置继续识别；`agent_limits.concurrency` 优先。旧 `collaboration_profiles` 不再参与限制计算，避免档位重新引入数量差异。恢复旧会话保留用量记录，但按当前宿主配置重新计算约束。Solo 始终禁止委派。

CLI 可使用 `xuloom exec --collaboration ultra "任务"`，Python 宿主可使用 `engine.set_collaboration_mode("auto")`。`engine.agents("capacity")` 返回的 `limit`、`available` 为 null 时表示未设置并发上限，不表示容量为零。框架未设置上限不代表机器或供应商提供无限资源。

## 桌面配置入口

设置 → Skills / Subagents 可以新建定义、选择全局或当前工作区范围、导入目录、刷新和查看指令。新建内容写入实际配置目录，不写进 EXE，不覆盖已有文件。MCP 服务页连接现有 MCP 管理器。没有会话时先新建一个独立会话，无需先配置模型。

打开项目后，在右侧 Skills / 代理面板点击“怎么配置”，可以查看当前项目和全局的真实目录、
文件模板，并打开对应目录。可点击“添加示例到当前项目”，同时创建 `project-review` Skill 和
`reviewer` 子代理；存在同名文件时会拒绝覆盖。面板支持刷新、查看指令或代理定义。

在任务输入框输入 `$` 弹出 Skill 列表；继续输入名称或描述筛选，↑/↓ 选择，Enter/Tab 确认，Esc 关闭。
列表直接扫描真实技能目录，显示名称、描述和来源，只列出有效且未被覆盖的技能。
选中后在输入行内以可移除的蓝色文字挂载，可继续输入 `$` 选择多个技能；发送时读取对应的完整 `SKILL.md`。
也可使用右侧“选择使用”。子代理在角色卡片中点“分配任务”，补全任务后发送。
模型需支持工具调用，才能实际启动子代理；导入一个定义文件本身不会启动任务。

## Skills

发现顺序为项目 `.xuloom/skills`、`.agents/skills`、`.claude/skills`、导入目录、
`XULOOM_SKILLS_PATHS`、用户目录下对应的三个位置，最后是随应用分发的 `xuloom/skills/builtin`。
同名的第一个有效的 Skill 生效，自定义技能可以覆盖内置技能。
所有格式正确的技能默认可用，无需启用开关；旧的停用配置不再影响可用性。界面显示无效和被覆盖的条目。

```text
my-skill/
  SKILL.md
  references/
  scripts/
  assets/
```

```markdown
---
name: my-skill
description: Describe when and how to use this skill.
---

Instructions go here.
```

名称应与目录名一致，使用小写 ASCII 字母、数字和单个连字符。导入会挂载已有目录，
不会复制文件；移动或删除原目录会影响使用。每次打开 `$` 列表都会重新扫描目录，也可在右侧点击刷新。
名称和描述进入模型上下文，完整指令与资源按需读取。选择“选择使用”会显式加载指令。
资源访问限制在 Skill 根目录；`allowed-tools` 等声明目前不构成权限授予。

可以导入 `examples/skills/project-review` 体验示例。

源码自带九个真实的技能目录：`code-development`（编程流程）、`code-review`（代码审查）、`debug-issue`（故障排查）、
`web-research`（联网研究与链接引用）、`skill-creator`（技能编写）、`data-analysis`（数据分析）、`document-writing`（文档写作）、`document-summary`（资料总结）、`experiment-design`（实验设计）。它们与用户技能共用发现和加载逻辑，同时适用于 CLI 和桌面应用。
安装包包含这些 `SKILL.md`，用户无需单独下载，也不会将内置技能复制到用户项目里。

## 自定义子代理

定义放在项目/用户的 `.xuloom/agents`、`.agents/agents`、`.claude/agents`，
也可以在代理面板导入目录。每个代理使用一个 Markdown 文件：

```markdown
---
name: reviewer
description: Review code independently.
model: inherit
tools: Read, Grep, Glob, Skill
skills: [project-review]
---

Inspect the task and report concrete findings. Do not modify files.
```

工具别名支持 Read、Write、Edit、Bash、Grep、Glob、Skill，以及实际工具名。
省略 `tools` 会继承可用工具集合；空列表表示不提供工具。
引用的 Skills 必须有效。自定义定义不能覆盖内置角色。

### 角色连接与异构原生子代理

设置 → Subagents → 新建角色，可选择模型设置中已有的供应商连接和模型。默认“继承主代理连接”保留原行为；选择另一连接后，`inherit` 表示该连接的默认模型，不再表示主模型。连接需先配置 Key（无鉴权本地模型可填 `not-needed`）。保存角色不调用模型。

CLI 使用相同 Markdown；`connection` 填 `config.json` 的 `model_connections[].id`，不是显示名称，也不要填 Key：

```yaml
name: independent-reader
description: 使用独立连接核对材料
connection: my-local-api
model: local-model-id
tools: [Read, Grep, Glob]
```

保持原有 Markdown 前言与正文格式。省略 `model` 或填 `inherit` 使用指定连接的默认模型；显式模型必须在该连接的列表内。通过界面创建时直接选择即可，不必手抄 ID。

原生 worker 的供应商、地址、网络方式、模型及输入/思考能力在入队时固定，角色文件随后修改不会改变已接受的任务。Key 不进入 worker 记录；执行/继续时从原连接重新读取，允许同一连接轮换 Key。连接删除、缺 Key、模型移除或连接配置改变会明确失败，不退回主代理、不改投另一地址，也不自动重跑已开始的副作用。恢复原设置后可继续；或创建新任务使用新设置。

相同地址和模型名也可能属于不同账户；worker 使用所选账户的能力声明，不猜另一账户，不继承主代理的思考强度或其他供应商的 fallback。跟进、排队、取消、保存与权限继续使用现有生命周期，不增加第二套调度器。Solo 仍不能启动这些角色。

此能力是“原生子代理选择不同模型连接”。SSH、远端工作目录及 Codex/Claude Code/Pi/DSH 外部 CLI 后端尚未实现；这些是持续目标中的独立部分。

本地验收（2026-11-01）：最终源码 Python 3.12 全量 421 项（420 通过、1 跳过），桌面逻辑 55 项通过；源码和原 EXE 的扩展设置 E2E 通过。构建 `20261101-120609` 的包内 Python 异构/生命周期/正式 CLI 共 24 项通过。协议请求均发往隔离本地服务，不调用真实供应商或消耗用户额度；私人 Skill 不参与打包。

子代理有独立消息上下文，使用共享项目文件系统，仍受工具权限控制。
子代理默认只接收分配的任务，角色指令和 Skill 放在系统提示中，不继承主会话的聊天请求。
需要参考主会话时可设置 `inherit_context=true`；历史仅供参考，当前任务仍由 `prompt` 指定。
前台与后台任务共用宿主容量；Auto / Ultra 默认不设并发上限，显式配置有限并发时，超出空闲槽位的已接受任务自动排队。代理面板可以查看结果和取消后台任务。
给多个子代理分配同一个任务时，模型应一次调用 `agent_tool(prompt="你好", description="问候测试", count=10)`。
`count` 支持 1–10；一次创建全部独立任务，并按可用并发容量请求模型，结果按创建顺序返回，卡片显示编号和启动时间。
不同任务可在同一回复中提交多个调用。模型在多轮回复中逐个调用仍会逐个创建；供应商的并发限额也可能让已启动的请求排队。
独立任务使用 `run_in_background=true`，让主代理和其他任务继续推进；最终汇总需要结果不代表必须先用前台调用阻塞主代理。仅在没有其他有用工作可推进、必须先取得这个结果时使用前台等待。后台任务通过 `agent_tool action=wait` 等待并收集真实结果，最后再综合回答。
子代理的计划和待办独立维护，不会覆盖主代理的计划和待办；文件修改仍共享项目，分配任务时应避免修改同一个文件。
`model` 可以是 `inherit` 或同一连接提供的模型 ID。

先导入示例 Skill，再导入 `examples/agents`，即可使用示例 reviewer。

## 向用户提问

模型调用 `ask_user_question_tool` 后，桌面显示可点击选项，CLI 显示编号选项；两端均支持自定义回答及多选。只在聊天中列出选项不会显示选择卡片。
一个请求可包含 1–4 道题，每题包含 2–4 个选项。例如：

```json
{"questions":[{"question":"你希望使用哪种方案？","options":[{"label":"最小修改","description":"解决当前问题"},{"label":"全面调整","description":"同时调整相关流程"}],"multiSelect":false}]}
```

`questions` 是问题列表，选项必须放在每题的 `options` 中。工具定义直接展开嵌套字段，并提供完整示例；无效参数在显示卡片之前被拒绝，错误回执包含字段路径和修正示例，沿用现有有次数限制的修正流程。框架不会猜测题目或替用户选择。
题目、选项标签不能为空，同一请求的题目及每题的选项标签不能重复。同一批次中的提问串行执行。完全访问权限也不会替用户回答，用户停止任务时取消待答请求。

## 移动会话到其他项目

将独立会话或项目会话拖到侧栏的目标项目，也可在会话菜单选择“移动到项目…”。
历史、过程记录、草稿、名称和置顶状态保留；后续文件操作、命令、Skills、子代理和 MCP 使用目标项目配置。
运行中的会话及子代理需要先停止。移动会话不会移动项目代码文件。
移动前会检查目标记录冲突，失败时保留原记录；移动后重启仍恢复到目标项目。
