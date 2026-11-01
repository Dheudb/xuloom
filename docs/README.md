# 文档导航

| 文档 | 内容 |
|---|---|
| [桌面 App](desktop.md) | 使用便携包、打开项目、设置模型、恢复历史 |
| [CLI](cli.md) | 安装、启动、命令和会话恢复 |
| [目标与调度](goals-and-schedules.md) | 目标预算与验收、正式非交互 CLI、延迟和重复唤醒 |
| [模型配置](models.md) | 支持的协议、本地模型与兼容边界 |
| [后台渲染与验证](browser-capture.md) | 隐藏窗口截图、原生图像输入、权限与 CLI 依赖 |
| [本机与 SSH 网页预览验证](remote-html-validation-2026-11-02.md) | 远端相对资源、App／CLI 读取、截图路径及版本／关闭边界 |
| [Skills 与子代理](capabilities.md) | 目录规范、发现顺序、导入与执行 |
| [MCP 与搜索](mcp.md) | 市场、配置、连接测试和内置搜索 |
| [项目架构](architecture.md) | 目录职责和运行流程 |
| [开发者基座严格检查](developer-base-audit.md) | 持续验收清单、证据与尚未完成的边界 |
| [持续目标与功能验收](product-roadmap.md) | SSH、异构代理、聊天导航、多媒体生成与双端 UI 的追加范围 |
| [仓库与分支状态](git-status.md) | 桌面分支标识、CLI 查询及只读边界 |
| [聊天深链接](chat-links.md) | 右键复制、桌面/CLI 打开、移动与归档边界 |
| [输出与来源](session-summary.md) | 本轮/会话的真实输出文件、来源链接、工具入口及 App/CLI/SSH 查询 |
| [实例连接绑定](runtime-connections.md) | 多供应商客户端、压缩器与子代理的连接归属、切换及恢复 |
| [媒体接口核对与计划](media-adapters-plan.md) | 官方协议现状、后续生成工具的边界与验收；尚非已实现功能 |
| [SSH 截图验证](remote-capture-validation-2026-11-02.md) | 执行端 Chromium、资源与 Cookie 边界、图像回传和 App/CLI 验收 |
| [Codex 异构后端验证](codex-backend-validation-2026-11-02.md) | 实际 App Server / Responses、线程恢复、权限和保存故障、App/CLI 与成品检查 |
| [外部 Agent 后端计划](external-agents-plan.md) | Claude/Pi/Codex 协议、权限和生命周期接入约束；DSH 及外部 SSH 完整联动仍在推进 |
| [工具发现与恢复验证](tool-discovery-validation-2026-11-02.md) | 实际 Pi／Claude 工具缓存、会话恢复、桌面控制管道及原安装验证 |
| [开发与打包](development.md) | 环境、测试、Windows 运行时与构建产物 |
| [框架排查记录](framework-audit-2026-10-30.md) | 工具回执、写后记录、Windows Shell、故障注入及验证边界 |
| [源码对照与加固](framework-source-comparison-2026-10-30.md) | Codex、Pi、DSH 与 Claude 公开资料对照，文件事务、进程和完整输出修复 |
| [输入与恢复加固](input-and-recovery-hardening-2026-10-30.md) | 模型输入声明、原生工具图片、文件撤销与模型配置事务 |
| [Ultra 主动委派校准](ultra-delegation-2026-10-30.md) | Codex 策略对照、非阻塞子任务指引与实际请求验证 |

普通用户从桌面说明开始；修改代码从开发说明和架构开始。

模型 Thinking 开关、effort 选择、CLI 用法及简化派发入口见 [模型思考控制](model-reasoning-2026-10-30.md)。

文件正文阅读、自动换行和目录栏调整见 [文件分栏修复](file-review-layout-2026-10-30.md)。

执行过程图标、图片缩略图与持久回看见 [执行过程与图片回看](tool-transcript-2026-10-30.md)。

## 远程执行

[SSH 远程执行](remote.md) 包含桌面聊天、交互/非交互 CLI、共享连接设置、聊天链接、离线导航恢复、[HTML 资源预览](remote-html-validation-2026-11-02.md)和[附件传输](remote-attachments.md)。[原生分布式委派](distributed-workers-plan.md)已通过本机加密 SSH 检查；外部 CLI 经 SSH、远端截图和跨平台原生验收继续按持续目标推进。
