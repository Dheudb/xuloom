# CLI

正式非交互任务使用 `xuloom exec "任务" --json`；目标续跑与定时唤醒见 [目标与调度](goals-and-schedules.md)。

读取当前仓库与分支：`xuloom git-status --cwd <目录> --json`；交互模式输入 `/git`。不调用模型，详情见 [Git 状态](git-status.md)。

聊天链接：`xuloom chat-link --cwd <目录> --session <ID>` 生成，`xuloom --chat "xuloom://chat/v1/…/…"` 在终端打开；交互模式输入 `/link`。详情见 [聊天深链接](chat-links.md)。

## 安装与启动

基础 CLI 需要 Python 3.9+。建议使用 Python 3.12 的虚拟环境，便于同时使用 MCP。

```bash
python -m venv .venv
# 激活虚拟环境后
python -m pip install -e ".[mcp]"
xuloom --help
```

进入要处理的项目目录后运行 `xuloom`。首次使用 `/model` 配置模型。
从源码安装的 CLI 需要本机 Python；Windows 桌面便携包的用户不需要配置 Python。

Windows 便携包也可以直接启动 CLI。在目标工作目录打开 PowerShell，使用解压目录的实际路径：

```powershell
& "C:\path\to\Xuloom-win32-x64\resources\python\python.exe" -I -m xuloom.main
```

这个命令使用包内 Python，不依赖构建机器上的解释器路径。

## 常用入口

```text
xuloom                         交互式会话
xuloom -p "解释这个项目"         带初始任务启动
xuloom --continue               恢复当前项目最近一次会话
xuloom --resume SESSION_ID      恢复指定会话
```

`-p` 是交互式会话的初始消息，不代表执行完自动退出的批处理模式。

| 命令 | 用途 |
|---|---|
| `/model`、`/model status` | 配置、查看模型连接 |
| `/palette` | 命令面板 |
| `/plan`、`/plan off` | 切换规划模式 |
| `/coordinator` | 多代理协调模式 |
| `/tools`、`/todos`、`/status` | 查看工具、待办及运行状态 |
| `/sessions` | 列出当前项目会话 |
| `/resume <ID>` | 恢复会话 |
| `/undo <revision_id>` | 确认撤销一次有备份的文件工具写入；后续已修改则拒绝覆盖 |
| `/quit` | 退出 |

Skills、子代理和 MCP 与桌面端共用引擎实现。CLI 在启动时读取项目 `.xuloom/mcp_servers.json`；
修改配置后重启 CLI 以加载。桌面可以在 MCP 市场中编辑这份配置。

`revision_id` 来自对应文件工具回执，备份保存在中央会话历史中。撤销只恢复该次工具写入前的文件字节，不清除对话，也不回滚 Shell、REPL、MCP 等外部副作用。范围与程序接口见 [输入与恢复说明](input-and-recovery-hardening-2026-10-30.md)。

## 输入 $ 选择技能

交互终端输入 `$` 自动弹出当前工作区、全局、导入及内置技能列表。继续输入名称或描述筛选，↑/↓ 选择，Tab 或 Enter 确认；确认后保留蓝色 `$skill-name`，继续写任务，再按 Enter 发送。可在同一条任务中选择多个技能。Esc 关闭候选列表。

每次补全从现有 SkillRegistry 重新扫描，只显示有效且未被同名覆盖的技能，与桌面共用指令加载机制；不会读取私人技能到公开包中。无交互终端时保留普通文本输入，可直接写 `$skill-name 任务`，或在非交互 `exec` 使用 `--skill NAME`。
