# 外部 Agent 后端：核对与接入约束

2026-11-02。此计划属于持续目标。[可选外部后端](external-agents.md) 已接入共享角色与现有 Runtime：Claude Code 使用受控 MCP/stream-json，Pi 使用 RPC，Codex 使用 App Server/Responses。Codex 当前专项见 [验证记录](codex-backend-validation-2026-11-02.md)；DSH 的最小 headless 接入已开始独立验证，见 [专项记录](dsh-backend-validation-2026-11-02.md)。现有原生角色可绑定不同模型连接并派发到 SSH 主机，不将其算作全部外部 Agent 已完成。

## 已核对的 Codex 接口

官方 [非交互模式](https://learn.chatgpt.com/docs/non-interactive-mode) 提供 `codex exec --json`，输出真实 thread、turn 和 item 事件，可用于一次性任务。进程退出和回答文本不能单独代替完整执行回执。

[App Server](https://learn.chatgpt.com/docs/app-server) 提供连接握手、thread 创建/恢复/分支、turn 与流式事件，以及命令/文件审批请求。它更适合需要连续跟进、用户问题和审批交互的客户端；应消费协议返回的身份，不让模型转抄。官方 [接入方式概述](https://developers.openai.com/blog/codex-as-a-platform) 同样区分一次性 exec 与长期交互场景。

初步 Codex 核对只读取官方文档。随后已通过当前安装程序 `0.156.1` 的 schema、App Server 和独立回环 Responses/MCP 验证真实线程、工具调用及正常续聊；没有读取账户凭据或调用真实供应商。Claude 已另外核对实际 CLI 2.1.267 的受控 MCP/stream-json 与续聊；Pi 已核对并实际验证 1.4.0 RPC/MCP、同一历史续聊及确认式停止；DSH 的具体协议仍需分别核对，不能从一种协议推断其他 CLI 行为。

## 薄后端而非第二套调度

- 复用现有角色定义、子代理队列、父子关系、通知、结果落盘和取消所有权；后端仅将已授权任务映射到外部协议事件。
- 角色明确选择 native 或可用外部后端。只调用用户已安装的程序，不下载或打包第三方 Agent；启动前校验依赖与协议能力。
- 保存原始后端身份、回合与完整事件记录，并映射到现有执行回执。结构化完成事件、进程结果和结果保存分别检查；断线/退出不能自动冒充完成。
- 恢复使用保存的真实后端身份。发送任务后断线时不自动重发；无法核对执行状态时返回结果未知。
- 跟进和停止作用于本任务拥有的外部连接/进程，不能按进程名结束用户其他应用或从历史 PID 猜所有权。

## 权限和凭据先于启动

- 父框架的 Plan、显式拒绝和询问规则不能因进入外部 Agent 而失效。仅转发外部审批弹窗不等于已覆盖父框架全部规则。
- 后端不能表达或强制当前策略、实际系统隔离不兼容，或协议版本不支持所需审批时，在启动任务前明确拒绝；不用提示词模拟安全约束，不偷偷降级到完全访问。
- 不以跳过审批参数授予任意宿主工具执行权。受控 MCP 可关闭外部 CLI 的重复询问，但必须保留 Xuloom 的真实授权入口，阻止未经控制的外部原生工具进入供应商请求和执行，并限定连接；目录隐藏或功能开关不能代替调用校验。用户选择 Ultra 只改变协作倾向，不改变外部执行权限。
- 不把主 Agent 的 API Key、模型地址或整份环境自动传给外部 CLI。各后端使用用户明确配置的连接或自己的授权方式，凭据不进入参数日志和会话深链接。
- 可执行文件与参数分开构造，prompt 通过协议或 stdin 发送，不拼成 shell 命令。解析程序路径时避免被项目目录中的同名程序抢先命中。

## 验收入口

App 和 CLI 共享角色配置、连接与回执；缺少程序、错误协议、拒绝审批、取消、断线、进程崩溃、结果保存失败及跟进恢复都需有检查。先用本地确定性协议进程验证机械行为；真实外部模型调用另行授权，不用替身测试宣称真实供应商长期可靠。

## 追加协议核对（2026-11-01）

本机 Codex CLI 为 `0.156.1`，已只读生成 App Server JSON Schema，未创建模型回合、读取账户凭据或使用接口额度。后端应按安装版本实际字段握手，不假设不同版本拥有相同实验接口。

官方 [Hooks 说明](https://learn.chatgpt.com/docs/hooks) 明确：前置工具 Hook 能阻止多数本地函数调用，但托管网页搜索及部分特殊路径不在相同覆盖范围；部分不支持的返回字段会使 Hook 失败并继续调用。仅增加一个前置 Hook，不能证明父框架权限已被完整实施。

因此 Codex 实际适配仍要验证工具覆盖、Hook/控制端失联、未知请求与配置漂移。原生 Xuloom 的强制委派入口不能被一个外部审批弹窗替代；不能表达当前限制时应在执行前报不支持，而不是自动放宽。此 Codex 核对是接入设计证据，不是 Codex 后端已经实现。

## App Server 接入前置核对（2026-11-02）

再次读取官方 [App Server 文档](https://learn.chatgpt.com/docs/app-server)：`dynamicTools` 与 `item/tool/call` 仍标为实验接口。安装版本 `0.156.1` 的只读 Schema 包含工具参数、真实 `threadId`、`turnId` 与 `callId`，可用于关联原生执行回执；不能仅用最终文字或退出码作为工具成功依据。

官方 [配置说明](https://learn.chatgpt.com/docs/config-file/config-basic)列出 `features.shell_tool`、Hooks、Apps 和子代理等独立开关。下一步需实际验证禁用配置后的全部工具请求，尤其文件、内置命令、托管工具与外部工具的边界；关掉一项不代表其他路径也受控。采用独立配置目录，不读取个人登录或启动项目扩展。响应转发也要单独实现 Responses 契约，不能把现有 Chat Completions／Messages 转发器换个地址就称为支持 Codex。

这一步只核对文档与已有只读 Schema，未启动模型回合，未新增运行依赖；Codex 后端仍未作为可用选项发布。

## 后续实现核对（2026-11-02）

上述前置核对后，App Server / Responses 适配已实现；实际 `0.156.1` 通过了独立回环 SSE/MCP 的正常执行、相同线程续聊和故障检查，源码 App 与正式 CLI 的跨进程恢复也已通过。当前实施、验收与安装状态集中记录于 [Codex 验证](codex-backend-validation-2026-11-02.md)。实现采用已有受控 MCP，不增加 dynamicTools 回调层；目录过滤之外也对完整工具调用做授权和身份校验。

DSH 仍需独立适配。其公开 [Agent API](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/core/agent/README.md) 区分进程内 initiator、Agent handle 和可替换 driver；跨进程传递所有权不能只依赖其进程内归属。接入时继续保留 Xuloom 的父子身份、原生回执和权限，不能仅启动命令后把退出码当成任务完成。

## DSH 实现进展（0.1.16）

上述 DSH 待适配项已按独立 headless 协议推进：使用已安装的 `0.2.1-rc.5`，最小显式 profile 只加载必要核心与受控 MCP，不挂载 base bundle、个人配置或遥测。正常跟进以真实 Session、v4 JSONL、已确认前缀与原生回执为依据；异常中断不自动重放。源码 App／正式 CLI 和故障专项已通过，最终全量、原安装与公开发行分别记录于 [本轮验证](dsh-backend-validation-2026-11-02.md)。此处保留前期依据，不将进程内 Agent API 冒充跨进程协议。
