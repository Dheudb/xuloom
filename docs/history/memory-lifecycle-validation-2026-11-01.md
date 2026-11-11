# 后台记忆生命周期：修复与验证

2026-11-01，v0.1.7。最终源码全量及原安装验证已完成，结果见下方。此前 v0.1.6 验证记录保持不变，阶段性定向结果不替代最终全量。

## 已复现与修复

初始三项故障注入均失败，日志 `desktop/build/memory-lifecycle-before.log`：

- 普通主会话的持久记忆任务在引擎关闭后继续等待模型，可在客户端关闭和 writer 释放后尝试写入。
- 持久记忆提取失败仍推进处理游标并清零待处理回合数。
- 会话摘要请求失败仍推进 token/消息游标并清零工具计数。

组件现在只保留一个拥有的后台任务。停止、关闭、切换模型、恢复/切换会话会作废旧请求；关闭会等待任务清理，再关闭客户端和保存/释放 writer。会话摘要复用同一组件绑定新会话，取消中的旧任务不会因替换对象而丢失归属。即使某扩展压住 `CancelledError` 后返回内容，已作废任务也不能提交写入。

提取使用调度时的深拷贝，保存的游标仅覆盖实际快照。提取期间新增的消息、工具调用及回合计数留给后续处理。有效空记忆列表表示本批没有应保存的事实；无响应、错误、无效 JSON 或写入失败均不表示处理成功。

文件与索引复用现有 `atomic_text` 写入；数据写入后还要保存现有会话检查点。检查点失败不确认游标，下一次可重试。同名记忆标题现在按前言行位置判断，修复原有文件迭代中调用 `tell()` 导致无法正确匹配、不断创建副本的问题。读取已有摘要失败不再当作空白模板覆盖原文。

移除了已被任务句柄替代的 `_extraction_lock` 和 `_extraction_in_progress`。没有新增管理器、模型循环、第三方依赖或全局提示词。原有记忆内容及自动提取策略保留；此修复不承诺模型提炼出的事实在语义上全部正确。

## 定向证据

- 修复初版三项复现 **3/3 通过**（`memory-lifecycle-r1.log`）。
- 增加快照、计数、原子写入故障、同名更新、检查点失败重试、模型切换与组件重绑后，记忆/关闭/领域压缩组合 **19 项通过，5.104 秒**（`memory-lifecycle-r2.log`）。
- 真实 SDK 对本地 HTTP 请求：暂停响应时停止引擎请求，确认任务终止且不推进游标；显式重试后成功。此检查不使用真实供应商、不消耗用户额度。
- 最新记忆及运行时连接组合 **19 项通过，7.182 秒**（`memory-lifecycle-r4.log`）。
- 中间组合 `memory-lifecycle-r3.log` 的有效 30 项通过，但调用了不存在的测试模块名 `test_model_connections`，因此整组报告 1 项导入错误。已改用仓库实际存在的 `test_runtime_connections`，保留错误日志，没有称该组全部通过。
- TypeScript/Vite 构建及 Node **68 项通过**；最终 Python 全量与原安装结果待补。
- 收尾停止修复后，记忆/关闭/运行时连接 **27 项通过，9.524 秒**（`memory-finalize-r1.log`）；最终原安装组合 **31 项通过，11.027 秒**（`memory-installed-python-final.log`），确认实际导入安装资源并走本地 SDK 请求。
- 原 EXE 最终外观及关闭流程通过（`memory-installed-final-appearance.log`、`memory-installed-final-shutdown.log`）。真实 Windows 文件共享拒绝仍会阻止错误退出，重试后保存且不重复模型请求。此前窄屏、滚轮/滚动条拖动、流式阅读和双侧宽度持久化通过（`memory-installed-scroll-layout-r2.log`）。
- 外观旧用例要求六个标签与已按用户要求移除的右侧目标面板，初次报告 `5 != 6`（`memory-installed-appearance.log`）；现检查五个真实标签及加号中的目标入口，没有恢复旧面板，也没有去掉布局/滚动断言。设置页遗留的免费额度说明已移除，最终前端构建及 Node 回归通过。

## 追加的收尾竞态

第一轮源码全量 **571 项，570 通过、1 跳过，644.451 秒**（`memory-final-full-r1.log`）后，继续构造“最终 Hook 等待时停止”的场景，发现停止仍可返回 `completed` 并调度新的记忆任务。单独复现日志 `memory-finalize-before.log` 保留。已在 Hook 返回后、后台任务调度及最终完成前重新检查停止信号；最后一段草稿保留，但回执必须为取消。该修复和新增用例之后重新执行最终全量，不能将前一轮 571 项作为最终版本通过证据。

## 最终结果及成品

- 最后一次生产代码调整后，Python 3.12 全量 **572 项，571 通过、1 跳过，640.829 秒**（`desktop/build/memory-final-full-r2.log`）。跳过为 Windows 符号链接权限限制。此后只补验证文档，不修改运行代码。
- 最终前端构建通过，Node **68 项通过**（`memory-ui-final-build.log`、`memory-ui-final-node.log`）。
- 包内 Python **31 项通过，11.027 秒**（`memory-installed-python-final.log`）；原 EXE 外观、关闭及原生 SSH App/正式 CLI 检查通过（`memory-installed-final-appearance.log`、`memory-installed-final-shutdown.log`、`memory-installed-final-ssh.log`）。前述滚动/宽度检查同样通过，未因外观旧断言而取消。
- 原安装构建 **0.1.7 / 20261101-204208**。逐字节核对源码及安装资源：152 个 Python/Skill 文件、17 个 Electron 文件、62 个前端文件相同；仅一个 `xuloom-0.1.7.dist-info`、九个公开内置 Skill，无 Paramiko/fal SDK。
- 稳定启动路径仍为 `desktop/release/Xuloom-win32-x64/Xuloom.exe`，入口 SHA256 保持 `2529b494f4123f152bce346051f7bd855ef95006424a561953cf17c49a3d1ba8`；更新回滚资源保留，没有新增功能 EXE。发行包以 `RELEASE.json` 绑定源码提交并附校验和，公开下载见 [v0.1.7](https://github.com/Dheudb/xuloom/releases/tag/v0.1.7)。

## 验收边界与持续目标

以上是任务所有权、保存与恢复机制的本地证据。没有做真实供应商长任务或物理跨机/macOS/Linux 原生验收。跨会话记忆的语义冲突、内容质量、提取频率及成本仍需继续审查；原子替换不等于跨会话事实合并协议。

外部 Codex/Claude Code/Pi/DSH 后端和全界面审查继续见[完整目标](../product-roadmap.md)。私人 Taiyi Skill 不读取、不修改、不上传、不打包。
