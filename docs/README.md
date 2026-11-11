# 文档导航

[当前能力与支持边界](current-capabilities.md)是当前版本的能力索引；本机构建的提交、构建号与验证范围可在设置中查看。过去版本的通过数字集中从[历史记录](history/README.md)查阅。

| 文档 | 用途 |
| --- | --- |
| [桌面 App](desktop.md) / [CLI](cli.md) | 安装、模型配置、会话与操作入口 |
| [模型连接](models.md) / [凭据](credentials.md) | 支持协议、Key、代理与能力声明 |
| [Skills 与子代理](capabilities.md) / [MCP](mcp.md) | 配置、发现、角色与工具连接 |
| [框架接口](framework.md) / [开发者扩展](extensions.md) | 公开程序入口、工具注册、事件、取消与恢复 |
| [插件](plugins.md) / [生命周期 Hook](hooks.md) / [模型适配器](model-adapters.md) | 打包扩展、在固定节点介入任务、接入任意模型 API |
| [HTTP 服务模式](server.md) | `xuloom serve`：会话、任务、SSE 事件流与远程审批 |
| [回放与评测](replay-and-eval.md) | mock 模型、录制/离线回放、`xuloom eval` 批量评测 |
| [追踪与花费](observability.md) | Span 导出（JSONL、OpenTelemetry/Langfuse）、模型价格与任务花费上限 |
| [目标与调度](goals-and-schedules.md) / [条件跟进](followups.md) | 持续任务、定时消息及已有回执驱动的继续 |
| [上下文与记忆](memory-and-compaction.md) | 压缩、归档、来源与调用开销 |
| [工具续跑](tool-sequences.md) / [原生续跑](native-continuations.md) | 已确定步骤的条件推进与中断边界 |
| [文件引用](references.md) / [来源与产物](session-summary.md) | 链接、文件、引用与历史查看 |
| [后台截图](browser-capture.md) / [媒体生成](media.md) | 按需能力、用户配置及环境条件 |
| [SSH](remote.md) / [外部 Agent](external-agents.md) | 远程会话、可选执行器及各自边界 |
| [Scaling](scaling.md) | 实验工作台，独立启用与显式用量确认 |
| [开发与打包](development.md) / [发布约定](release-process.md) | 本地全量、构建追溯、升级与回退 |
| [更新日志](../CHANGELOG.md) / [安全策略](../SECURITY.md) | 每个版本的用户可见变化；私密报告漏洞的方式 |

普通使用不要求配置 SSH、Scaling 或外部 CLI。真实模型长任务、物理跨机、跨平台成品与系统级强隔离分别验收，不由历史本地协议测试自动推出。

运行中补充要求、撤回与 CLI 输入流：[运行中追加指令](live-input.md)。

完整 ZIP 的体积精简和依赖保留：[打包体积](packaging-size.md)。
