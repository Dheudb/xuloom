# 聊天深链接

桌面侧栏右键聊天，选择 **复制聊天链接**。也可在会话菜单的省略号中找到。复制不会新建副本或切换当前聊天。

本机链接形如 `xuloom://chat/v1/<工作目录标识>/<会话 ID>`；SSH 链接形如 `xuloom://chat/ssh/v1/<已配置连接目标标识>/<会话 ID>`。地址不含工作目录路径、主机名、消息、API Key 或命令。它不是云端分享或 SSH 配置导入：本机地址要求对应本机历史，远端地址要求本机已配置匹配的 SSH 目标，再由远端提供历史。

## 打开

- 桌面：设置 → 工作区 → 聊天链接，粘贴并点击“打开会话”。应用回复中的有效 Markdown 聊天链接也可直接点击，不交给外部浏览器。
- Windows 便携包：在同一页点击“注册系统链接”，为当前用户注册此 EXE。注册成功后，其他应用点击链接可启动 Xuloom 或跳回已运行的主窗口。移动解压目录后应在新位置重新注册。
- CLI：`xuloom --chat "xuloom://chat/…"`，打开本机或已配置的 SSH 会话；不自动发送提示词，也不接受链接中的提示词或权限参数。SSH 对话继续复用远端原运行时，不在本机目录启动替代引擎。

注册操作是用户显式点击后调用 Electron 系统协议 API，不在每次启动时改写默认应用。Linux 需要桌面文件和默认协议的安装集成；当前 Windows 便携包不能证明 Linux/macOS 原生链接可用，macOS 原生包尚未提供。未注册、未安装程序或系统不支持时，使用粘贴打开或 CLI。没有程序的电脑不能单靠 URI 安装或下载程序。

普通多窗口工作台仍可同时存在；带 URL 的第二次启动将地址转给主实例后退出，不另外打开空白窗口。导航复用当前会话切换路径，先保存草稿；不抢夺执行中的会话写者锁，不自动批准操作。后台路径首次加载会话也向前端发送已有的初始化结果，随后才发送运行事件；导航响应不再用旧初始化视图覆盖已收到的实时输出。定时投递等待前端初始化完成。

## CLI 与程序接口

```text
xuloom chat-link --cwd <目录> --session <会话ID>
xuloom chat-link --resolve "xuloom://chat/v1/…/…" --json
xuloom --chat "xuloom://chat/v1/…/…"
xuloom chat-link --remote lab --session <远端会话ID> --json
xuloom chat-link --resolve "xuloom://chat/ssh/v1/…/…" --json
xuloom --chat "xuloom://chat/ssh/v1/…/…"
```

交互终端输入 `/link` 输出当前已保存会话的地址；尚未保存的聊天会提示找不到，不为生成链接创建假历史。`chat-link --json` 沿用 JSONL 最终回执；解析返回 `chat.id/session_id/project/scope/archived/moved/workspace_exists`，不返回聊天正文。`--resolve` 只查询，即使已归档或工作目录丢失也返回状态；真正打开时必须恢复归档和缺失目录。无效或不存在的地址退出码为 1，`result.reason` 提供 `invalid_link/not_found/archived/workspace_missing/history_unreadable/ambiguous_link` 等明确原因。

正式非交互 CLI 的标准输入、输出和诊断明确使用 UTF-8，不依赖 Windows 活动代码页或 `PYTHONIOENCODING`。调用方也应按 UTF-8 解码；测试刻意从 ASCII 配置启动子进程，核对中文路径仍能往返。

Python 接口为 `xuloom.features.chat_links.chat_link(cwd, session_id)` 和 `resolve_link(url, require_openable=False)`。前者只编码标识；后者验证实际中央历史。桌面内部 RPC `chat.resolve` 使用同一解析器，不创建 QueryEngine；桌面 `chat.open` 负责呈现和切换。

远端生成函数为 `remote_chat_link(profile, session_id)`，只编码连接目标与会话 ID。Python `parse_link` 仍返回 `(key, session_id)`；本机 key 为 16 位、SSH key 为 64 位。目标标识按规范化的连接 ID、主机别名、目录、Python、平台、端口和本机 SSH 配置路径计算，显示名称不影响地址。因此改名称后链接仍有效，改执行目标后旧链接不会自动改投新目标。另一台电脑即使能访问同一服务器，也需要匹配的连接配置；地址不能替代 SSH 凭据或自动匹配任意别名。

`chat-link --resolve` 与 Python/RPC 解析 SSH 链接只读本机配置，不连接 SSH，也不声称远端历史一定存在。其结果包含 `transport=ssh`、`connection_id`、`session_id`、经过校验的公开连接绑定、已有机器身份（如果工作台曾保存）及 `remote_verified=false`，没有草稿、消息或模型 Key。`chat-link --remote lab --session ID` 会显式连接该已配置目标并检查已有会话可读，成功后返回 `remote_verified=true`；不会创建替代会话。真正打开时再进行 SSH 主机密钥核对、已保存机器/目录身份检查和远端历史读取。

终端远端聊天支持 `/link`、`/status`、`/git`、`/skills`、`/agents`、`/files`、`/read`、`/model list|status|use N`、`/thinking enabled|disabled`、`/effort LEVEL`、`/permission ask|full`、`/collaboration solo|auto|ultra`、`/sandbox host|workspace`、`/plan on|off`、`/continue` 和 `/quit`。`$` 补全读取远端技能快照。选择题和审批要实际输入后才回传；空输入不默认批准。Ctrl+C 停止本连接拥有的任务，`/continue` 使用远端原回合恢复，不追加合成提示词。非交互批处理继续使用 `remote exec --json`；`remote chat --json` 在连接前明确拒绝。

## 边界

- 严格接受本机 v1 与 SSH v1 地址，拒绝查询参数、片段、凭据、编码路径、其他 scheme 和超长 ID。链接不能指定 Shell、工作目录或新权限。
- 拖动聊天移动项目后，旧地址通过现有 `workspace_moves` 记录查到新位置；不会只因另一项目有相同 session ID 就误开它。分支副本不是原会话的替身。歧义明确报错。
- 会话/项目归档后要求用户先恢复；删除后提示不存在。工作目录丢失时保留历史并报错，不新建空目录或空会话。损坏或外链历史文件也不会被当作新聊天。
- 从侧栏移除的项目保留历史，已有有效链接可重新显示它；删除历史的操作不会被链接撤销。
- SSH 连接删除、目标更改、引用损坏时明确失败，不按同名主机或目录猜测其他目标。已归档的远端导航要求先恢复；远端记录删除或不可读时不会创建新会话。
- 远端断线报告结果未确认，不自动重发 prompt，也不落回本地。终端在等待输入或选择题时断线同样退出等待，不留下阻塞标准输入的线程；管道输入明确按 UTF-8 处理，`/link` 不人工插入换行。

## 本地验证

`tests/test_chat_links.py` 覆盖参数、只读查询、迁移身份、归档/丢失/损坏、真实 CLI 子进程、CLI 恢复参数和无模型 RPC。`desktop/tests/chat-links.test.cjs` 验证 URI/启动参数合同。隐藏 Electron 的 `npm run test:chat-links` 验证右键复制、设置粘贴、Markdown 内部跳转、草稿、移动后的旧链接、归档、删除、目录丢失、启动 URL 和真实第二进程转发，模型请求计数必须为零。测试不会注册或覆盖开发机的系统默认协议。

系统协议入口参考 [Electron app API](https://www.electronjs.org/docs/latest/api/app) 的 `open-url`、`second-instance`、`requestSingleInstanceLock` 与 `setAsDefaultProtocolClient`；不假设这些 API 等于所有平台已有完整安装集成。

2026-11-01 本机链接阶段：Python 3.12 全量 404 项（403 通过、1 跳过）；桌面逻辑 55 项通过，前端构建通过。源码与原 EXE 的聊天链接 E2E 均通过；源码的双窗口偏好、目标/定时投递及网页链接回归通过。包内 Python 的链接、CLI 所有权和非交互任务 15 项通过，UTF-8 子进程解码不再出现代码页错误。原安装更新至 `20261101-024805`，没有新建 EXE，九项内置技能中不包含私人 Taiyi。测试未调用远端模型、未更改开发机默认协议；系统事件注入和实际第二进程转发不冒充已完成原生安装/注册点击的各平台验收。

后续 SSH 地址与终端验收使用加密 SSH 回环和隔离本地 HTTP/SSE。源码桌面流程覆盖复制/粘贴/启动打开、草稿、归档、删除、目标改变与无模型请求导航；原本机链接流程继续覆盖真实第二进程转发与 Markdown 内部跳转。终端用正式子进程和真实管道输入检查中文、打开历史、生成/解析链接及退出，用同一 SSH 运行时检查选择题、显式拒绝、停止/继续、空闲断线和远端 `$` 补全来源。不把这些证据当作两台物理机器、原生 ConPTY 键盘或 macOS/Linux 安装注册已经验证。

该阶段最终验证：Python 3.12 串行全量 446 项（445 通过、1 跳过），桌面逻辑 61 项及前端构建通过。源码本机链接和 SSH 桌面 E2E 均通过；原 EXE 的 SSH 桌面 E2E、以包内 Python 为远端/正式 CLI 的加密检查 19 项通过。本地构建 `20261101-134005`，136 个 Python/技能文件和桌面资源与源码一致，九项公开内置 Skill 不包含私人 Taiyi，运行时没有加入 Paramiko，稳定 EXE 字节未变。此开发阶段尚未同步 GitHub/Release。

后续已完成导航恢复检查并公开 [v0.1.2](https://github.com/Dheudb/xuloom/releases/tag/v0.1.2)，源码提交 `2942c53`、本地构建 `20261101-142836`。最新验证结果与早期失败记录见 [审查记录](history/developer-base-audit.md#ssh损坏导航的离线恢复)。
