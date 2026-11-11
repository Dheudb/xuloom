# 原生远端执行：本地验证记录

范围：原生角色共用装配、强制委派权限、受控恢复、专用 worker RPC、真实系统 OpenSSH 与回环 SSH 服务。后续已接入原生队列、消息/跟进/跨频道取消，以及 App/正式 CLI 配置和历史导航，见[实现与使用](distributed-workers-plan.md)。下方基础阶段结果保留为历史证据，不能当作后续改动的最终全量或已发布声明。

## 原生派发阶段（v0.1.6）

- `test_native_ssh_workers.py` 使用实际 AgentTool/FIFO、系统 OpenSSH 与加密回环服务器，模型 SDK 请求本地 HTTP/SSE。覆盖宿主 Key 与模型绑定、三任务容量为一时自动排队、父权限拒绝、远端独有 Skill、目标变更拒绝、保存失败前不授予模型预算、显式输出限额与确认取消。
- 活跃流中的消息直到真正消费并保存才清除本机邮箱；最后一段输出期间到达的消息会在结束前消费。空闲代理消息保留到显式跟进，不自动启动模型。新增这一实际流与跟进检查 **2 项通过，13.030 秒**（含旧后台 Shell 所有权），`desktop/build/native-ssh-message-r1.log`。
- 旧后台 Shell 现在按 agent 所有权过滤和清理。验证取消一名子代理只终止其实际拥有的测试进程，兄弟任务继续；从历史恢复的 PID 不作为杀进程授权。
- 源码隐藏 App 与正式 CLI 的同一角色端到端通过：图形选择 SSH/主机模型、实际派发与真实回执、远端完整历史深链接、受控历史写入拒绝、共享 Markdown 角色。`desktop/build/native-ssh-ui-r6.log`。此结果使用本地协议服务，不是跨物理机器或真实供应商验收。
- 生产回归组合最后一轮 **56 项通过，118.186 秒**（`native-ssh-controls-r5.log`）；此后又增加消息消费修复，最终全量结果另行记录，不把这 56 项当作最终提交全量。
- **最终源码 Python 3.12 全量：561 项，560 通过、1 跳过，658.288 秒**，`desktop/build/native-ssh-full-r1.log`。跳过为 Windows 符号链接权限限制。本轮原生执行与消息代码在这次全量开始前已完成；随后仅补包版本一致性检查和隐藏截图方式，版本检查通过实际 wheel 构建/安装验证。
- Node 最终逻辑回归 **68 项通过**，`desktop/build/native-ssh-final-node.log`；TypeScript/Vite 构建通过（`native-ssh-final-build.log`）。仍有已有大 chunk 提示，未把它伪装成构建失败或取消检查。
- **原安装 Python 契约 56 项通过，225.820 秒**，`native-ssh-installed-python-r1.log`。测试进程、SSH 执行主机和正式 CLI 均使用安装包内 Python，检查导入路径确实指向安装资源；开发用 Paramiko 只从测试环境加载，未进入安装包。
- 原 EXE 的新原生 SSH App/CLI 检查通过（`native-ssh-installed-ui-r2.log`）；旧完整 SSH 工作台和本地 40 worker 批量/后台流程也通过（`native-ssh-installed-workbench.log`、`native-ssh-installed-subagents.log`）。原安装第一次新 UI 流程在功能断言通过后，隐藏页面截图超时；测试改用 Electron `capturePage({stayHidden, stayAwake})` 后完成截图及后续 CLI 验证，未显示窗口或终止用户应用。
- 单一路径 `desktop/release/Xuloom-win32-x64/Xuloom.exe` 已更新资源为 **0.1.6 / 20261101-195917**，入口 EXE 的 SHA256 保持 `2529b494f4123f152bce346051f7bd855ef95006424a561953cf17c49a3d1ba8`。源码与包内 **152 个 Python/Skill 文件、17 个 Electron 文件、62 个前端文件**逐字节相同；仅一个 `xuloom-0.1.6.dist-info`、九项公开内置 Skill，无 Paramiko/fal SDK。更新回滚资源保留。
- 第一次资源预构建检测到旧 `setup.py` 的版本未同步；该资源未安装或发布。修复版本，并增加 Python wheel、模块和桌面版本一致性拦截后重新构建，成品校验通过。发行包通过现有发布脚本绑定干净源码提交与校验和；公开版本及下载见 [v0.1.6](https://github.com/Dheudb/xuloom/releases/tag/v0.1.6)。

初次新增组合中的重复关闭错误和一次错误测试模块名均保留在 `native-ssh-controls-r1.log`；关闭幂等和调用名称已经修正。UI 初次检查还发现 `remote.models` 缺少 IPC 路由，已补并以实际 App 请求通过验证。其他早期 E2E 选择器/异步按钮等待失败日志同样保留，没有把重跑成功当作首次成功。

## 基础阶段（先前提交）

## 已检查的实际路径

- `test_delegated_authority.py` 走真实 QueryEngine 工具准备/执行入口：拒绝时文件不写入；本机/远端 Full Access、只读或显式允许仍需要委派入口。显式询问、工具别名、Plan、限制性祖先、审批版本变化、配置损坏和丢失控制端不能变成许可。普通 CLI 恢复受控会话不请求模型；切换普通聊天清除旧绑定。
- `test_role_runtime.py` 检查明确空工具集、动态/只读/MCP 工具过滤、团队工具不能绕过禁止、冻结指令在 Skill 更改或删除后恢复、参考上下文不增加工具权限。
- `test_remote_worker_protocol.py` 检查稳定身份、角色/父任务变动检测、严格字段与真实布尔值、明确引用上下文；主机控制记录只保存所有权摘要，不保存原始控制 token 或 API Key。
- `test_remote_worker_bridge.py` 同时使用原生 SDK 的本地 HTTP/SSE 和实际加密回环 SSH。检查一次真正的模型流、执行主机自身 Key、完整落盘回执、重连与重复 run ID 不重发请求、不同输入拒绝、首次保存失败不启动、损坏/被改动的控制拒绝、必需工具/MCP 缺失拒绝、绑定变化不重定向，以及不暴露其他会话历史。
- SSH 真实写文件调用在收到父权限前保持等待；父进程拒绝后实际文件不存在。另一个频道可以查占用，但不能替换原执行，也不能谎称已停止；实际拥有者停止后，第二频道读到保存的取消状态。同一 run ID 不重新执行。

## 本轮结果与边界

- 最后一次端点定向组合 **30 项全部通过，32.469 秒**：`desktop/build/remote-worker-host-r6.log`。更早的失败与通过记录保留，没有用重跑抹除初次问题。
- Node 逻辑检查 **68 项全部通过**：`desktop/build/distributed-foundations-node.log`。
- 隐藏、隔离配置的源码桌面子代理 E2E 通过：多调用和单次批量调用均先发出十个请求再返回；覆盖前台/后台与共 40 个不同 worker。日志 `desktop/build/distributed-foundations-subagents-ui.log`。这是现有本地原生子代理回归，不冒充远端队列端到端。
- 隐藏、隔离配置的完整 SSH 工作台 E2E 通过：真实加密聊天、远端模型/Key、文件、Git 分支、实际用户选择、深链接、本机引用移除并重新打开、忙碌移除保护、草稿/重启、本机与远端分离、分支/归档/恢复/删除、断线重连不重复提交。日志 `desktop/build/distributed-foundations-ssh-ui.log`。
- **最终源码 Python 3.12 全量：549 项，548 通过、1 跳过，558.010 秒**，日志 `desktop/build/distributed-foundations-full-r1.log`。跳过为当前 Windows 的符号链接权限限制；在最后一次生产代码调整（移除无调用者的私有旧协调模式）后执行，没有拿此前版本的全量结果充当本轮通过。

初次原地测试发现配置路径在模块导入时已绑定，单改环境变量没有隔离已导入的配置模块，曾误读本机模型配置并尝试连接，连接失败。测试现同时替换实际配置路径，并在启动前核对回环模型地址；后续测试仅使用本地服务与测试凭据。HTTP header 的大小写、错误控制打开后的关闭、委派任务额外记忆请求等初次失败也已保留并修复。私人 Taiyi Skill 没有读取、修改、上传或打包。

没有完成两台物理主机、macOS/Linux 原生、真实远端模型长任务、外部 Codex/Claude/Pi/DSH 的验收。除上述配置隔离失败的连接尝试外，修复后的检查只使用回环服务和测试凭据，不消耗供应商配额。开发用 Paramiko 只供测试 SSH 服务，不是应用运行依赖或分发内容。未增加新的调度状态机、远程常驻服务、模型权重或全局提示词。
