# SSH 远程执行（开发版本）

使用系统 OpenSSH 连接另一台安装了 Xuloom 的机器，复用现有桌面 JSONL 运行时、QueryEngine、中央历史、权限与子代理生命周期。不安装常驻服务，不复制本机模型配置，不在产品里保存 SSH 密码或私钥，不新增 SSH SDK 运行依赖。

当前已经实现：远端交互/非交互 CLI、历史/文件/Git 查询、程序 RPC，以及桌面连接管理、远程聊天、HTML 预览、[附件传输](remote-attachments.md)、[原生分布式子代理](distributed-workers-plan.md)和 [SSH 聊天链接](chat-links.md)。外部 CLI 后端经 SSH、远端截图及原生跨平台验收仍未完成。设置中“测试成功”不等于全部能力已验收。完整范围见 [持续目标](product-roadmap.md)。

## 准备远端

远端需要 SSH 服务及安装了本开发版本 Xuloom 的 Python。先在远端源码目录执行 `python3 -m pip install .`，也可在虚拟环境中安装；本机和远端都需要包含本页所述的 `runtime.info` 协议。旧公开 v0.1.1 尚不包含此 SSH 协议，不能仅升级本机后期待旧远端兼容。

在本机终端先用 `ssh my-lab` 验证登录和主机指纹。Xuloom 使用 `BatchMode=yes`、`StrictHostKeyChecking=yes`，不自动信任新主机、不弹密码框；可复用系统 SSH agent、密钥文件及 `~/.ssh/config` 主机别名。支持别名里的跳板机配置，关闭 agent/X11/端口转发，不复用后台 ControlMaster；你的 SSH 配置及 ProxyCommand 本身属于受信任的用户配置，不能当作对恶意本机配置的沙箱。

远端自己配置模型 API、Skills、MCP 与角色。没有配置远端模型也可以检查握手、查看远端文件和历史；提交任务时需要远端模型可用。

## 添加连接

桌面：设置 → **SSH 远程** → 新增连接，填写主机别名、远端绝对工作目录及 Python 路径，保存后测试。Linux/macOS 选择 POSIX，Windows 选择 Windows。Windows 远端通过 PowerShell 编码命令启动；不会拼接用户输入为额外 Shell 命令。

CLI 使用同一个本机 `config.json` 的 `ssh_connections`：

```sh
xuloom remote add lab --host my-lab --cwd /home/me/project --python /home/me/venv/bin/python --json
xuloom remote list --json
xuloom remote check lab --json
```

`lab` 是连接 ID。`--name` 设置显示名称；`--port` 显式覆盖主机配置中的端口，省略则使用 OpenSSH 配置。`--ssh-config` 可指定本机已有配置文件的绝对路径。Windows 示例：

```powershell
xuloom remote add winlab --host win-host --platform windows --cwd 'C:\work\project' --python 'C:\venv\Scripts\python.exe' --json
```

这些操作不上传源码、模型 Key、私钥或私人 Skill。工作文件仍在远端工作目录，历史保存在远端用户的中央数据目录。

## 桌面聊天

设置 → SSH 远程 → **新建远程会话**，或先点 **查看远端会话** 后选择已有记录。侧栏以服务器图标区分远程工作区；顶栏显示 SSH 主机、实际机器名和远端 Git 分支。输入、停止/继续、选择题、工具审批、Skills、原生子代理、模型选择及文件树/文本/差异沿用本地工作台组件，实际请求经 SSH 发往远端现有运行时。

本机仅保存连接绑定、会话导航引用、显示名称、置顶/归档标记和草稿；聊天正文、模型 Key、工具结果和产物保存在远端。当前远端会话的重命名/归档是本机工作台展示设置，不会修改另一台客户端的导航偏好。复制为新会话或从回复分支、永久删除会话通过远端已有存储事务执行，收到确认后才更新本机引用；删除会话不会删除工作目录中的产物。

在远端会话中打开设置，模型与扩展配置写到远端；设置标题显示 SSH 目标。SSH 连接管理和默认本机工作目录仍属于本机设置。切回本机会话不会沿用远端模型。已有会话绑定完整连接目标（主机、目录、Python、端口、SSH 配置文件），仅改显示名称可继续使用；连接目标改变、远端机器名/平台/解析目录改变时不会自动改投新目标。

重启会恢复上次选中的远端聊天和未发送草稿，不会重新提交任务。右键可复制 SSH 深链接，在桌面粘贴或通过 `xuloom --chat URL` 打开。断线显示执行结果未确认；重新选择该会话会重连并读取远端已保存状态，不自动重发 prompt，也不接管其他进程的写锁。仍在运行的工作区需先停止才能关闭。附件可以上传到远端会话、预览图片或下载副本；当前打开远端资源管理器式路径入口及远端截图仍不可用，不会把远端路径当成本机同名路径打开。网页预览使用下述受限资源读取。

## 网页预览与资源读取

本机与远端均更新到 `0.1.12` 或支持 `files.htmlInfo`／`files.htmlChunk` 的后续版本。在远端会话的文件面板选择 HTML，切换“预览”即可运行页面；相对 CSS、JS／模块脚本、图片、字体、JSON、WASM 和媒体资源通过同一 SSH 连接分块读取。原有 iframe 隔离、独立预览来源与页面 CSP 保留，不提供远端端口代理，也不调用模型。

执行端检查真实文件路径、资源类型及大小，每个资源最多 30 MB；隐藏路径、目录穿越、指向工作区外的链接均拒绝。工作区外但由该会话实际修改的 HTML 只能预览该文件，不能读取邻居资源。读取期间文件版本变化会失败，需要刷新；断线不改用本机路径。关闭预览或会话会撤销预览地址。

CLI 查询同一入口，不在终端假装渲染网页：

```sh
xuloom remote preview lab pages/report.html --json
xuloom remote preview lab pages/report.html --asset assets/data.json --offset 0 --length 262144 --json
```

默认返回资源元数据；指定 `--offset` 后返回一个 Base64 分块及真实版本、偏移和总大小，单次最多 512 KB。资源路径相对远端工作区，`--session` 可加载某个会话的已保存文件范围。交互远端 CLI 提供 `/preview <HTML path>` 元数据查询；完整文本可继续使用 `/read`。这些查询不启动模型或自动续跑任务。旧远端缺少协议时明确失败，应先升级远端。

远端定时消息沿用远端持久化调度记录。桌面仅向仍连接、已打开、未归档且空闲的匹配会话投递到期消息；断线、关闭、忙碌时保留待投递状态。若需要断开桌面后仍唤醒，在远端运行原有 CLI 唤醒服务，不假设 SSH 连接是常驻任务守护程序。

## 执行与恢复

交互聊天：

```sh
xuloom remote chat lab
xuloom remote chat lab --session SAVED_ID
xuloom --chat "xuloom://chat/ssh/v1/…/…"
```

进入后显示实际机器、工作目录与会话 ID，读取远端历史；打开链接不会自动发送 prompt。`$` 选择远端 Skill，`/model list` / `/model use N` 选择远端模型；`/permission ask|full`、`/thinking enabled|disabled`、`/effort LEVEL` 等沿用远端能力约束。选择题与审批必须实际回答，不能因 Full Access 就默认回答问题或跳过显式询问。Ctrl+C 请求停止当前连接拥有的任务，`/continue` 继续原停止回合，`/quit` 关闭本连接；断线不自动重连或重发任务。命令列表见 [聊天链接与终端](chat-links.md)。

批处理和程序接入使用原有非交互命令：

```sh
xuloom remote exec lab "检查这些资料，给出证据和结论" --json
xuloom remote sessions lab --json
xuloom remote inspect lab --session SAVED_ID --json
xuloom remote exec lab "根据已有结论继续" --session SAVED_ID --json
xuloom remote exec lab --session SAVED_ID --resume --json
```

`--resume` 仅继续已有的停止回合，不追加合成 prompt，也不能同时传新 prompt。只在已有回合确实被停止/中断时使用。`--skill NAME` 选择远端技能；`--collaboration solo/auto/ultra` 调整远端原生协作。

默认 `--permission ask`。非交互 CLI 不会假设批准，也不会替用户回答问题：需要批准或输入时停止并返回 `approval_required/input_required`（退出码 3）。显式 `--permission full` 仍遵守远端的显式拒绝/询问规则；它不提供系统级沙箱。

JSONL 先报告真实机器和会话 ID，然后才提交可能产生副作用的任务；只输出一个最终回执。执行结束和最终存档完成分别检查。`session_busy` 返回退出码 4，连接中断或超时为 `remote_disconnected`、退出码 9，并标记 `outcome_unknown=true`。该状态不代表远端一定停止或任务一定失败。此实现没有自动重发 prompt、自动接管写锁或按历史 PID 杀进程。

Ctrl+C 请求当前连接拥有的停止，只有收到远端确认才报告 `stop_confirmed=true`。SSH stdin 关闭会触发现有远端运行时的清理；网络异常时清理可能延迟，必须重新连接后查看占用和保存状态。另一个连接的 `stop` 不会取消已有写者的工作。远端进程或机器强制退出后，已有不确定副作用仍按原恢复规则检查，不能宣称 exactly-once。

## 读取远端产物

```sh
xuloom remote files lab --json
xuloom remote read lab report.md --json
xuloom remote git-status lab --json
xuloom remote remove lab --json
```

文件查询沿用工作区的目录/文本预览约束。删除连接配置不删除远端文件或历史。当前远端截图不会静默改为截取本机浏览器；没有连接远端渲染器时明确失败。

## 清理本机远端导航

设置 → SSH 远程 → **本机 SSH 导航引用** 可检查或移除本机缓存的会话引用。损坏的引用不会使本机历史列表不可用；列表上方的“处理 SSH 导航问题”可直接打开恢复界面，即使 SSH 连接配置损坏也能使用。此入口只读取本机设置，不连接 SSH，也不请求模型。

```sh
xuloom remote references --json
xuloom remote forget REFERENCE_TOKEN --version SNAPSHOT_VERSION --confirm --json
```

`REFERENCE_TOKEN` 和 `SNAPSHOT_VERSION` 使用上一条命令返回的 `id`、`version`，不是聊天 ID 或 SSH 连接 ID。版本变化时拒绝移除，防止旧界面删除刚更新的草稿。原始条目先保存在中央数据目录的 `navigation-backups/*.json`，备份失败则保留原引用；这些备份也可能包含草稿，应按私人聊天数据保管。

**移除本机引用**同时移除它的本机草稿与导航选中状态，但不删除远端历史、远端文件或 SSH 连接配置。工作区显示设置的移除只重置本机显示，不移除会话。之后仍可从已配置连接的“查看远端会话”重新打开真实历史。本机原有“删除会话”则是另一项实际远端删除操作，不能混用。

桌面会拒绝移除本窗口正在运行的会话，并在移除前关闭本窗口持有的空闲连接。CLI 清理只修改本机导航，不探测或终止其他窗口、进程或机器的任务；不要把它当作取消命令。若关闭连接后版本校验发现其他写入，引用继续保留，刷新后重试。

整个 `workbench.json` 已不是合法 JSON、无法读取或根对象损坏时，仍保留文件并明确报错；此恢复入口针对合法设置文件中的远端条目/容器，不宣称能自动修复任意存储损坏。

## 程序接口

```python
import asyncio
from xuloom.remote.config import resolve
from xuloom.remote.ssh import SSHClient

events = asyncio.Queue()
client = SSHClient(resolve("lab"), on_event=events.put_nowait)
try:
    machine = await client.start()
    created = await client.request("sessions.create")
    await client.request("initialize", {"sessionId": created["sessionId"]})
    await client.request("prompt", {"text": "你的任务"})
    # 从 events 读取真实事件；turn_finished 之前检查 final/turn_error。
finally:
    await client.aclose()
```

`on_event` 是快速同步回调，例如 `asyncio.Queue.put_nowait`，不能阻塞读循环。请求超时不自动重试；重新连接后的历史/占用查询不创建模型请求。

## 验收范围

`tests/test_remote_ssh.py` 使用本机系统 OpenSSH、临时密钥及主机密钥、仅监听 127.0.0.1 的加密 SSH 服务，再启动真实远端桥接进程；模型请求只访问隔离本地 HTTP/SSE。覆盖握手、Windows 路径中的空格/中文/引号、错误主机密钥、远端凭据与历史归属、活动任务断线、停止所有权、并发写者、显式恢复和非交互提问。Paramiko 仅为可选的开发测试依赖，未加入运行依赖或成品；缺少时对应 wire 测试明确跳过。

这是真实 SSH 回环协议证据，不是两台物理机器、真实科研长任务或 Linux/macOS 原生部署验收。POSIX 命令构造需要继续做原生远端验证。桌面 `test:remote-settings` 检查实际配置界面与正式 CLI 读取同一份配置。检查未使用用户真实 Key，不调用远端模型，不修改或打包私人 Taiyi Skill。

2026-11-01 CLI/设置阶段检查：Python 3.12 全量 433 项（432 通过、1 跳过），桌面逻辑 55 项通过；源码提问、停止/恢复和 SSH 设置 E2E 通过。原 EXE 构建 `20261101-123406`，同一安装的 Python 作为 SSH 远端及正式 CLI 的加密回环检查 11 项通过，安装版 SSH 设置 E2E 通过。源码与包内 134 个 Python/技能文件逐字节一致，仍只包含九个公开内置 Skill，运行时未加入 Paramiko。

桌面聊天阶段检查：源码和原安装的 `test:remote-workbench` 加密回环均通过，包含真实用户选择、文件写入/读取、模型及 Key 分离、分支显示、重启/草稿、聊天分支/归档恢复/删除、活动断线及重连无重发。桌面逻辑 60 项通过（包括断线后清除子代理转圈并保留已完成结果）；模型设置、滚动/宽度 E2E 通过。最终 Python 3.12 串行全量 435 项（434 通过、1 跳过），安装运行时 SSH 回环 12 项通过；构建 `20261101-131900`，135 个 Python/技能文件及桌面资源逐字节一致。首次并行验收中有一项 Windows 原生隔离文件操作超时，独立模块与后续串行全量通过，仍保留时序风险记录；安装版首次隐藏截图超时，源码截图及安装版全部操作流程分别核对，详见 [审查记录](developer-base-audit.md#ssh桌面聊天工作台)。本阶段没有调用真实远端模型或发布 GitHub/Release，不能替代两机和其他平台原生验收。

聊天链接/终端阶段检查：最终 Python 全量 446 项（445 通过、1 跳过）、桌面逻辑 61 项，源码本机链接、源码/安装版 SSH 桌面 E2E 及安装运行时 SSH/CLI 19 项通过；当前本地构建 `20261101-134005`，136 个 Python/技能文件及桌面资源一致。新增内容与当前限制见 [聊天链接](chat-links.md#本地验证)；公开代码和 Release 仍待同步。

导航恢复阶段：v0.1.2 最终 Python 3.12 全量 457 项（456 通过、1 符号链接权限跳过）、桌面逻辑 64 项与构建通过。最终原安装 `20261101-142836` 的恢复界面、包内导航/CLI 11 项及完整加密 SSH 工作台通过；移除引用后原远端历史仍能重新打开，没有新增模型请求。较早失败及两个准备构建的证据区分保留在 [审查记录](developer-base-audit.md#ssh损坏导航的离线恢复)，没有用一次针对性重跑代替最终全量。

上述已验证功能已随 [v0.1.2](https://github.com/Dheudb/xuloom/releases/tag/v0.1.2) 公开，源码与标签为 `2942c53`，下载 ZIP 和校验文件均已上传。两机/跨平台及后续远端能力仍按持续目标验收。

参考：[OpenSSH 命令手册](https://man.openbsd.org/ssh.1)、[OpenSSH 配置手册](https://man.openbsd.org/ssh_config.5)、[Paramiko 测试服务接口](https://docs.paramiko.org/en/stable/api/server.html)。
