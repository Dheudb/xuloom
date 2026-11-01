# Xuloom

一个运行在你电脑上的通用智能体工作台。连接你自己的模型，通过桌面或 CLI 完成研究、写作、数据分析和开发任务；用 **Skills** 复用工作流程，用 **Subagents** 分工，用 **MCP** 连接外部工具。

[English](README.en.md) · [下载 Windows 版](https://github.com/Dheudb/xuloom/releases) · [完整文档](docs/README.md)

## 先用起来

1. 在 [Releases](https://github.com/Dheudb/xuloom/releases) 下载 `Xuloom-v版本号-windows-x64.zip`，完整解压，运行其中的 `Xuloom.exe`。不要只复制 EXE，也不要把 GitHub 的 Source code 当成安装包。
2. 打开 **设置 → 模型连接**，填写 API 名称、协议、地址、Key 和模型 ID。一个 API 可以配置多个模型，也可以添加多个供应商。
3. 点击 **新建会话**，直接输入任务。每个独立会话都有自己的工作目录；处理已有文件夹时再点 **打开项目**。
4. 需要添加能力时，打开 **设置 → Skills / Subagents / MCP 服务**。不需要先调用模型，也不要求工作区是 Git 仓库。

Windows 下载包自带 Python 和 MCP SDK，无需安装 Python、选择解释器或安装 Node.js。第三方 MCP 程序可能另需 Node.js、uv 等环境，按服务说明准备。

当前源码版本为 **v0.1.14**，可下载成品及校验和以 Releases 为准。成品面向 Windows x64；Linux/macOS 可使用源码 CLI，尚无对应的桌面发行包。

执行过程按操作显示图标和简短说明：图片、编辑、终端、搜索等各有标识，点击过程行可展开参数与结果，完成后整轮过程自动折叠。读取图片会显示可点击放大的缩略图；会话保存图片快照，原文件被修改或删除后仍能回看。预览本身不会调用模型；模型理解图片还需所连接的接口支持图片输入。

**后台截图检查：** 可以要求“渲染这个 HTML／打开本地开发服务，截图检查布局”。模型通过 `browser_tool` 的 `render` 操作，在隐藏窗口加载页面并直接收到截图；过程里也能放大回看。桌面版复用自带 Chromium，无需额外安装浏览器；CLI 需安装 `pip install "xuloom[browser]"` 并执行 `python -m playwright install chromium`。该工具沿用权限审批，系统隔离模式下仍禁用浏览器；不支持图片输入的模型只能取得截图路径，不能进行视觉检查。详见 [后台渲染与验证](docs/browser-capture.md)。

## 模型怎么配

Xuloom 支持 **OpenAI 兼容 Chat Completions** 和 **Anthropic Messages**。本地推理服务可使用它提供的兼容接口。地址填写服务的 API 根地址，例如 `http://localhost:8000/v1`，模型 ID 使用服务实际提供的名称。

在“可选模型列表”中每行填写一个模型，或点击“从 API 获取模型”，保存后从输入框右下角切换。Key 只保存到本机配置，不随项目发布。无鉴权本地服务可填占位值；云服务必须使用自己的有效 Key。

文本问答成功不代表该模型完整支持工具调用、图片或推理字段。Xuloom 不提供模型额度，也不保证任意兼容接口都支持全部能力。详见 [模型配置](docs/models.md)。

## 远程使用与会话导航

**设置 → SSH 远程**可添加另一台机器的 SSH 连接、远端工作目录与已安装 Xuloom 的 Python 路径。桌面可打开真实远端会话、切换远端模型、处理审批和问题、查看文件与 Git 分支。CLI 使用同一份连接配置：`xuloom remote chat 连接ID`，程序化执行使用 `xuloom remote exec 连接ID "任务" --json`。凭据和历史留在远端，不会自动把本机 Key 上传过去。

SSH 会话的 HTML 可直接在文件面板预览，支持工作区内的相对 CSS、脚本、图片和数据资源。资源由远端校验并通过 SSH 分块读取，不转发网页端口、不调用模型；CLI 使用 `xuloom remote preview 连接ID 页面路径 --json` 查询同一资源。两端需更新到支持此协议的版本，详见 [远端网页预览](docs/remote.md#网页预览与资源读取)。

右键会话可复制聊天深链接；CLI 可生成、解析或打开同一地址。SSH 链接选择本机已配置目标，不是跨账户的云分享链接。离线或损坏的本机引用可以在 SSH 设置里移除，不删除远端历史。详见 [SSH 远程](docs/remote.md)、[聊天链接](docs/chat-links.md) 与 [实例及角色模型绑定](docs/runtime-connections.md)。

**原生分布式子代理：** 在 **设置 → Subagents → 新建** 选择执行位置（本机或已配置 SSH 主机）、对应主机的模型连接及模型。角色仍由现有队列派发，支持跟进、消息、结果和拥有者确认的取消；无后台命令的已完成 worker 会释放 SSH 连接。远端历史和文件可以从代理卡片打开，受控历史不能绕过主会话执行。App 与 CLI 共用角色文件，执行主机需安装兼容的 v0.1.6 worker 协议；不会复制本机模型凭据或 Skill 文件。详见 [分布式子代理](docs/distributed-workers-plan.md)。

可选的 [Claude Code / Pi / Codex 后端](docs/external-agents.md) 可使用用户已安装 CLI，继续走 Xuloom 的原生队列、工具授权和中央历史。Claude 使用 Anthropic Messages；Pi 支持 Anthropic Messages 和 OpenAI Chat，Codex 使用 OpenAI Responses。Pi 主动停止在回执与历史确认后可通过同一 worker 明确续聊，未知结果不会自动重放，见 [停止与恢复边界](docs/external-agents.md#续聊停止与边界)。DSH 外部后端及外部 CLI 经 SSH 的完整联动仍在 [持续目标](docs/product-roadmap.md) 中，不能把原生 SSH worker 当成所有后端已验收。

按需发现的工具会随会话保存，续聊时恢复当前角色仍允许使用的工具；晚注册的 MCP 工具也重新检查角色范围。外部 CLI 缓存工具定义不等于获得执行权限，模型请求和实际执行仍受原生可见性及授权约束。详见 [工具发现与恢复验证](docs/tool-discovery-validation-2026-11-02.md)。

## 图片、视频、语音与音乐

在 **设置 → 媒体生成** 添加服务、填写自己的 Key，再添加模型。一条连接可以共用一个 Key 配置图片、图片编辑、视频、语音、音效和音乐模型。当前实现 **fal Queue API** 及同协议服务；普通聊天 API 地址不能直接替代这个协议。保存配置不会调用服务。

配置后可直接在对话中描述要生成的内容，主代理通过已有工具发现入口调用 `media_tool`。产物保存在会话工作目录的 `generated/`；点击文件或“输出与来源”可查看图片、播放音视频。生成工具提供真实任务身份，只有文件可靠保存后才返回完成；停止本机等待不会自动取消供应商任务。

**设置页可查询或请求取消已有任务**，CLI 使用 `xuloom media status/cancel`，这些操作不调用聊天模型。提交结果未知时不会自动重新生成；重启后继续查原任务。完整配置示例、支持的参数与故障边界见 [媒体生成](docs/media.md)。实现使用现有 HTTP 依赖，不打包供应商 SDK、模型权重或私人 Skill。本地协议与播放验收不等于真实供应商可用性或生成质量验收。

## Skills：把方法变成可复用流程

Skill 是一个包含 `SKILL.md` 的文件夹，可以附带脚本、参考文献和素材。它提供做事的方法，本身不等于新工具或额外系统权限。

**用界面添加：** 在 **设置 → Skills** 点击 **新建 Skill**，填写名称、使用时机和执行指令，选择“全局”或“当前工作区”后保存。已有技能点击 **导入目录**，选择包含 `SKILL.md` 的目录或技能集合目录。导入挂载原文件，不会复制；不要随后移动或删除原目录。

**使用：** 在输入框输入 `$`，搜索并选择技能，再填写任务发送；也可以在右侧 Skills 面板选择。有效技能默认可用，不需要逐个打开开关。

**手动配置：** 以下内容保存为 `.xuloom/skills/literature-review/SKILL.md`：

```markdown
---
name: literature-review
description: 检索并比较某个主题的公开文献，给出可核对的引用。
---
先明确研究范围，再检索原始来源。区分论文结论、你的推断和待验证问题。
输出比较表、关键分歧与可点击的来源链接。找不到证据时明确说明。
```

名称须与文件夹名一致，使用小写字母、数字和单个连字符，最多 64 字符。其他资源可放入同级的 `references/`、`scripts/`、`assets/`。

- **当前工作区：** `<工作目录>/.xuloom/skills/`，仅该目录下的任务使用。
- **全局：** `~/.xuloom/skills/`，所有工作区可发现。
- **内置：** `code-development`、`code-review`、`debug-issue`、`web-research`、`skill-creator`、`data-analysis`、`document-writing`、`document-summary`、`experiment-design`，随公开程序分发。

自己的技能放在用户或项目配置目录，**不用重新打包 EXE**。同名技能按发现顺序取第一个有效定义；无效或被覆盖的条目会显示原因。兼容目录及完整规则见 [Skills 与子代理](docs/capabilities.md)。

## Subagents：定义角色，再把任务交给它

Subagent 使用独立消息上下文执行分配的任务。角色可以指定职责、工具和 Skills；它仍受会话权限、协作额度和模型接口限制。

**用界面添加：** 在 **设置 → Subagents → 新建 Subagent** 填写角色指令。供应商连接默认继承主代理，也可选择已配置连接及其模型；工具留空继承可用集合，Skills 填已存在的技能名称。已有定义可通过 **导入目录** 挂载。

**手动配置：** 将下列内容保存为 `.xuloom/agents/source-checker.md`；放到 `~/.xuloom/agents/` 则所有工作区可用。

```markdown
---
name: source-checker
description: 独立核对给定材料的证据和引用。
model: inherit
tools: Read, Grep, Glob, Skill
skills: [literature-review]
---
核对分配给你的材料，列出已证实、证据不足和相互矛盾的主张。
报告来源与理由，不擅自修改文件。
```

先添加示例依赖的 `literature-review` Skill。`tools` 可使用 Read、Write、Edit、Exec、Grep、Glob、Skill 等别名或实际工具名；省略 `tools` 继承工具，手动填写 `tools: []` 表示禁用全部工具。省略 `connection` 时，`model` 使用主代理连接；添加 `connection: <已配置连接的 ID>` 可跨供应商运行原生子代理。桌面与 CLI 共用定义和凭据，详见 [角色连接](docs/capabilities.md#角色连接与异构原生子代理)。[原生 SSH worker](docs/distributed-workers-plan.md) 使用执行主机的连接和配置；可选的 [Claude Code / Pi / Codex 后端](docs/external-agents.md) 使用用户已安装 CLI 和受控 MCP 工具，不捆绑外部程序或使用个人登录。DSH 外部后端仍待接入；Codex 需要真正支持 Responses 的连接，不能将 Chat Completions 网关视为已经兼容。

在右侧“代理”中点角色的 **分配任务**，或在 prompt 中明确指定角色。创建定义不会自动启动代理。输入框中的 **Solo** 关闭委派，**Auto** 按需委派，**Ultra** 更积极地寻找并行机会。两档默认不设子代理数量限制；只有显式设置宿主并发限制时，超额已接受任务才排队。详见 [协作与配置](docs/capabilities.md)。

## SSH 远程（开发版本）

开发版本已加入 [SSH 桌面聊天与 CLI](docs/remote.md)：使用另一台机器自己的模型、Skills、MCP 和历史。设置 → SSH 远程可创建或打开远端会话；CLI 可运行 `xuloom remote chat lab --session <ID>` 交互聊天，或 `xuloom remote exec lab "任务" --json` 非交互执行。停止、选择题、审批和模型切换仍由远端原有运行时处理，不上传本机 API Key。

右键会话可复制[本机或 SSH 聊天链接](docs/chat-links.md)，桌面粘贴打开，或 `xuloom --chat "xuloom://chat/…"` 在终端打开。远端链接只选择已配置连接，不导入主机或自动发送 prompt。SSH 现已支持[附件传输与副本下载](docs/remote-attachments.md)、HTML 预览和[原生远端子代理](docs/distributed-workers-plan.md)。可选 [Claude Code／Pi 后端](docs/external-agents.md)有各自的本地验证范围；Codex／DSH、外部后端经 SSH、物理两机与跨平台等剩余工作见[完整目标](docs/product-roadmap.md)，不把回环协议测试算作全部部署验收。

## MCP：连接工具与外部服务

打开 **设置 → MCP 服务 → 打开 MCP 管理**，也可从侧栏进入 MCP 市场：

1. 从市场选择服务，或点击 **自定义服务**。
2. 本地服务选择 **stdio**，填写命令、参数及环境变量；远程服务选择 **Streamable HTTP**，填写 URL 和请求头。
3. 保存后先 **测试连接**，检查握手、工具发现及依赖是否正常。
4. 服务默认停用，确认后启用；新任务会连接，也可以手动刷新连接。

配置保存在 **当前工作区** `.xuloom/mcp_servers.json`，桌面和 CLI 共用。例如一个本地文件工具服务：

```json
{
  "mcpServers": {
    "local-files": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/allowed-folder"],
      "env": {},
      "enabled": false
    }
  }
}
```

上例需要本机 Node.js/npm，并将允许目录改成真实路径。远程配置使用 `type: "http"`、`url`、`headers`。凭据使用环境变量或请求头，避免写入会显示在预览中的命令参数。连接测试会实际启动程序或访问服务，但不会调用聊天模型。

仅启用可信服务。当前未实现 MCP OAuth 登录和旧版 SSE。Windows 系统隔离模式会禁用 MCP。详见 [MCP 配置与搜索](docs/mcp.md)。

## 会话、文件和权限

- 新建会话拥有独立工作目录；项目内的“＋”使用对应项目目录。拖动会话到其他项目只改变后续工作的目录，不搬运原文件。
- 聊天记录集中保存，项目 `.xuloom` 用于扩展与配置。CLI 与桌面共用存储；同一会话只允许一个执行写者，没有实时双向编辑同步。
- 输入框选择 **请求批准** 或 **完全访问**。完全访问仍遵守显式拒绝和模式约束，不等于操作系统沙箱。
- 可选的 **＋ → 执行隔离** 使用 Windows x64 LPAC，限制已接入工具进程的写入、网络与进程能力。worktree/目录副本只隔离文件改动。具体限制见 [执行隔离](docs/queue-and-isolation.md)。
- 输入框 **＋ → 目标** 将下一条 prompt 作为持续目标，运行后显示目标条；目标条支持调整目标、暂停、恢复和取消；桌面目标不设累计输出、轮数和时间预算。定时唤醒从“＋”单独进入。模型宣称完成不等于领域验收通过。

| 内容 | 保存位置 |
| --- | --- |
| 模型连接与 Key | `~/.xuloom/config.json` |
| 全局 Skills / Subagents | `~/.xuloom/skills/`、`~/.xuloom/agents/` |
| 工作区扩展与 MCP | `<工作目录>/.xuloom/` |
| Windows 历史与独立工作区 | `%LOCALAPPDATA%/Xuloom/` |
| Linux 数据 | `$XDG_DATA_HOME/xuloom/`，未设置时为 `~/.local/share/xuloom/` |
| macOS 数据 | `~/Library/Application Support/Xuloom/` |

配置与凭据保存在本机，模型和外部工具仍会接收任务中提供给它们的内容。公开仓库及发行包不包含个人配置、私人技能或聊天记录。

## CLI 与源码开发

开发建议 Python 3.12+、Node.js 24+ 和 Git；CLI 基础功能兼容 Python 3.9+，MCP 需要 Python 3.10+。本地搜索使用 `rg`，请安装并加入 PATH。

```powershell
git clone https://github.com/Dheudb/xuloom.git
cd xuloom
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[mcp]"
# 交互 CLI
.\.venv\Scripts\xuloom.exe
# 桌面开发
cd desktop
npm ci
npm run dev
```

macOS/Linux 激活 `.venv/bin/activate` 后使用 `xuloom`。批处理使用 `xuloom exec "任务" --cwd /path/to/work --json`，执行后自动退出并返回明确回执和退出码。桌面包自带运行时的 CLI 启动方式见 [CLI 文档](docs/cli.md)。

| 进一步了解 | 文档 |
| --- | --- |
| 模型、代理网络和接口兼容 | [模型](docs/models.md) |
| Skills、角色字段、作用域、协作额度 | [扩展](docs/capabilities.md) |
| 原生子代理生命周期与程序接入 | [执行接口](docs/framework.md) |
| 目标、非交互 CLI、定时唤醒 | [目标与调度](docs/goals-and-schedules.md) |
| 测试、构建与项目结构 | [开发](docs/development.md) · [架构](docs/architecture.md) |

自动更新、签名安装器与 Linux/macOS 桌面成品尚未提供；真实模型的小时级长任务与跨平台可靠性仍需持续验收。GitHub Actions 当前仅手动运行。本地测试方法见开发文档。

## 许可证

[MIT](LICENSE)
