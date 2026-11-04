# Scaling 本机参与与均衡分组（2026-11-04）

v0.1.30 在原 Scaling 工作台中增加“添加本机”。本机节点不需要 SSH；使用已保存的模型连接、同一节点服务、持久化记录、权限、资源准入和恢复机制。勾选参与才计入本次容量。本机与多个 SSH 节点可以混合选择。

## 分组与执行身份

worker 容量与组长分开计数。每组上限默认 50；组数为容量向上除以组宽，worker 均匀分配、任意两组最多相差 1。51 → 25＋26，60 → 30＋30，100 → 50＋50，101 → 33＋34＋34；1000 → 20 × 50。

新运行由总主 agent 规划，再直接交给各执行节点上的独立组长。每个组长的展开、汇总和分组复核复用自己的原生主会话，直接管理 worker，不额外启动一层只负责传话的节点主模型。总主 agent 在本机；本机配置 100 时另有 2 个组长与 100 个 worker；SSH 节点配置 100 时，远端是 2 个 Xuloom 组长会话与 100 个 worker。组长是后台会话，不需要打开多个桌面窗口。

新运行检查节点的 `group_leaders` 能力；旧运行缺少 `grouped_leaders` 标记时保留原结构，避免恢复中改变执行身份。供应商离线额度仍按真实分组预分配与释放，同一组内原生模型会话共享组额度；供应商 HTTP 限流继续按真实连接共享，不能因为创建多个组长而绕过总上限。

## 本地证据与边界

- 前一轮 Scaling／层级／SSH 组合回归 45 项通过（189.213 秒）。随后按用户要求增加均衡边界、旧节点能力拒绝，以及分组并行额度验证。
- 源码隐藏 Electron 检查通过：无 SSH 配置时可添加本机、保存 100 容量，显示 2 个组长，不重复添加本机；原 SSH 节点、模型配置、对话改向及较窄布局继续通过。
- TypeScript／Vite 构建通过。没有新增依赖，也没有修改普通 Solo／Auto／Ultra 的提示词。
- 100 个 worker 的用例使用确定性模型替身，核对 2 个组长会话、100 个 worker、1 个总主会话，不能作为真实百并发模型性能证明。
- 原生 SDK／本地后台节点服务测试不使用 SSH、不调用远端模型。小规模真实 HTTP/SSE 检查不同组长的主会话身份以及多组 worker 可在任一个返回前同时进入本地模型服务。
- 最终源码组合回归 **62 项通过（214.243 秒）**：`tests.test_scaling_local`、`tests.test_scaling_hierarchy`、`tests.test_scaling_ssh`、`tests.test_scaling`、`tests.test_request_flow`，完整日志为 `scaling-local-final.log`。包含两个组长在同一连接下的分组配额，以及四个 worker 在任一回复前同时进入本地 HTTP 模型服务的握手验证。

## 原成品

更新原 `desktop/release/Xuloom-win32-x64/Xuloom.exe` 的应用／Python 资源至 **0.1.30，构建 20261104-191133**，状态 `applied`，保留回滚。EXE 本体不另建，私人 Skill 不打包。

原 EXE 的隐藏 Electron 检查通过（`scaling-local-installed-ui.log`）。使用更新后包内 Python 作为远端执行器的加密 SSH 回环 **2 项通过（54.031 秒）**，覆盖新的分组会话、断连后继续、主 agent 对话与改向。201 个公开 Python／Markdown 包资源与源码字节一致；仅九项公开内置 Skill。包内 CLI 帮助包含 `--local`。

包内 Python 以隔离模式加载已安装的 v0.1.30，本机专项 **6 项通过（85.812 秒）**（`scaling-local-installed-runtime.log`），包括后台原生服务、独立组长主会话、四个 HTTP 请求的并行握手、100 个替身 worker 的身份数量、均衡边界、旧节点能力拒绝和暂停改向／取消。私人 Taiyi Skill 不包含在发布范围；发布版本及附件以 GitHub Releases 为准。

日志位于本机 `desktop/test-results/scaling-local-*.log`。本轮不调用真实供应商、不连接外部机器；没有千级真实模型、跨物理机器或 macOS／Linux 桌面性能结论。资源准入不是操作系统硬隔离。容量为 worker 工作上限，实际模型请求并发仍受 API 额度及主机资源限制。
