# 更新日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。1.0 之前的次版本可能包含不兼容变更，均在下面注明。v0.1.40 及更早版本的说明见 [GitHub Releases](https://github.com/Dheudb/xuloom/releases) 与 [历史记录](docs/history/README.md)。

## [未发布]

### 新增
- 交互式 CLI `/review`：用内置 `code-review` Skill 审查当前未提交改动、指定提交或范围，`--` 后可写审查重点。范围在发送前由 Git 校验并固定为完整哈希，无改动或范围无效时不请求模型。

## [0.1.51] - 2026-11-13

### 新增
- Windows 本机 Computer Use：可选工具支持授权窗口截图、移动光标、点击、输入、滚动和常用按键。默认关闭，桌面和交互式 CLI 共用。

### 优化
- OpenAI 兼容流默认等 8 分钟接收首个有效响应。超过 30 秒先显示中英文等待提示。界面语言可在设置窗口顶部切换。

## [0.1.50] - 2026-11-13

### 新增
- 桌面右侧的上下文标签页用分段条和分类表展示消息、系统工具、MCP 工具、系统提示、记忆及可用空间。待加载工具单独计数，展开可查看 MCP 工具和记忆文件。
- 新建模型连接默认自动选择直连或系统代理，记住每条 API 连接的成功线路。连接失败后尝试另一条可用线路，获取模型列表与对话共享记录。

## [0.1.49] - 2026-11-12

### 新增
- 只读会话工具 `chat_read_tool`：模型可按用户提供的聊天链接分页读取其他项目的已保存历史。工具按需加载，不增加默认 Prompt；CLI `transcript --link` 使用同一入口。SSH 只连接已配置目标，不初始化或重放会话。

## [0.1.48] - 2026-11-12

### 修复
- 普通会话不再受隐藏的 200,000 累计输出 Token 上限限制；保留历史用量统计，旧会话超过该值或因该限制停止后仍能继续。模型单次输出限制、显式目标／委派预算和费用上限继续生效。
- 无输出预算时使用空值而非无穷大，避免恢复事件产生非标准 JSON；显式预算耗尽返回明确中文说明。

## [0.1.47] - 2026-11-12

### 修复
- 无标题会话按界面语言显示默认名称；分支请求显式传递空标题，修复无标题会话分叉失败，并保留远端用户主动命名的“新会话”等原文。
- 上下文窗口按官方连接、已核对的开源模型原生规格或模型列表元数据识别；DeepSeek 官方模型不再落入未知模型的 64K 默认值，且不会因此切换协议或推理参数。
- “从 API 获取模型”同时提取 `max_model_len`、`context_window` 等明确窗口字段并填入空配置，保留手动窗口；不把最大输出长度当作上下文，不发送生成请求探测容量。
- 子代理汇合前通过现有邮箱发送一次收尾请求；原生只读调查连续重复读取同一证据时先请求汇总，仍无进展则以 `blocked` 返回并保留记录，不用时间或请求总数截断正常研究。
- 等待子代理立即保存检查点，模型流结束不再显示成任务完成；临时稿标明阶段性内容，最终回答仍须等实际子代理回执汇总。
- 网页抓取显式使用连接配置的直连／系统代理，避免搜索与抓取使用不同线路；不修改用户保存的网络设置。
- Apple DocC 官方文档 JSON 提取正文、代码与正文引用链接，避免把庞大导航索引当作文章反复分页读取；普通 JSON 保持原样。
- Ultra 闲聊允许普通文本直接完成，不再强制调用 `ultra_answer` 或因缺少评估回执触发重试；复杂任务仍提供 `ultra_prepare` 规划入口，原有回执和权限继续生效。
- OpenAI 兼容 Chat 的 Ultra 初始协作评估改用 `tool_choice=auto`，由现有执行循环验证评估回执；避免 DeepSeek 思考模式拒绝 `required` 导致 HTTP 400，保留思考和协作约束。
- 保存并按原连接与模型回传接口实际返回的 `reasoning_content`，覆盖工具回合、普通回复、JSON 会话恢复与录像；不作为回答/活动文字展示，不跨地址、Key 或模型回传。旧记录已丢失的字段不能自动重建。
- 已知的强制工具选择、缺少 `reasoning_content` 错误显示明确原因，仍不回显服务端原始错误、密钥或 URL。

## [0.1.46] - 2026-11-12

### 新增
- 桌面端显示费用：每轮回复下显示 token 和费用（没有价格时标为“未计价”，服务端未返回用量时标为估算）；新增 **设置 → 费用**，可编辑模型价格和每个任务的花费上限（保存在 `~/.xuloom/pricing.json`，与 CLI 共用，已打开的会话从下一次请求起生效）；“上下文”面板显示本会话费用。本地模型把价格填 0 即可在设置上限时正常运行。
- 桌面端英文界面：**设置 → 工作区 → 界面语言 / Language**。组件用 `t()` / `tx()` 翻译自己的界面文字（键是中文原文），会话标题、项目名、连接名、文件名和用户、模型内容只作为占位符进入句子，不会被翻译；后端提示用 `tServer()` 翻译已知消息。`npm run i18n:check`（包含在 `npm test` 中）检查未包裹的界面文字和缺少的英文，`scripts/i18n-codemod.mjs` 自动包裹新文字。
- `pricing.json` 可以包含 `max_cost_usd`，作为没有设置 `EngineConfig.max_cost_usd` / `XULOOM_MAX_COST_USD` 时的任务上限。
- `@tool`：把带类型注解的函数变成工具（参数结构来自类型注解，说明来自文档字符串）。`extra_tools`、`register_tool` 和插件的 `add_tool` 现在也接受工具类，并为每个引擎各建一个实例。
- `xuloom plugins new <名称>`：生成可直接运行的插件模板，附带不需要 API Key 的离线测试。
- 英文文档：[入门](docs/en/getting-started.md) 与 [扩展指南](docs/en/extending.md)。

### 变更
- 英文界面的 Git 不可用原因经过后端消息翻译，缺少 Git 时不再残留中文；仓库路径和分支名保持原文。
- 发布回归夹具修正两处竞争：队列检查等待实际请求到达，固定 OAuth 回调端口保留到登录前显式交接，避免被其他程序占用；失败时保留授权界面和服务诊断。
- 存储的会话和连接名称始终原样显示，包括用户主动选择的“新会话”“我的 API”等名称，避免误判为可翻译的默认名称。
- 执行隔离设置里的说明更新为已提供 Linux / macOS 实验后端。
- 花费上限的说明改为准确描述：它是停止条件，达到后不再发出新请求；已发出的请求（包括并行子代理的）仍会计费，最终账单可能超过上限。设置页、README 和[追踪与花费](docs/observability.md)同步更新。
- mypy 现在也检查 `QueryEngine`、全部 `engine_parts` 混入类和 `model_stream.py`（46 个文件，原 30 个）：生成的 `xuloom/core/engine_state.py` 声明混入类共用的属性与方法签名（运行时是空类），`scripts/gen_engine_state.py` 生成、`tests/test_engine_state.py` 检查是否过期。修正了检查发现的注解问题（隐式 Optional、容器类型等），没有行为变化。
- 桌面测试 `sessions.e2e.cjs` 的“新会话复用空白草稿”断言已过时（61f44b3 起每次点击都新建会话），改为检查新会话；`models`、`cost`、`i18n` 测试清除 `ANTHROPIC_*` 环境变量，避免宿主环境里的 Key 触发旧配置迁移。
- 桌面测试 `activity`、`permissions`、`questions` 的模拟模型仍按“请求里有没有 `agent_tool` / `ask_user_question_tool` 的结构”区分主代理和子代理、读取提问工具结构；主代理改为精简工具集（这些工具在 `tool_search_tool` 后面延迟加载）后，这个假设不再成立。现在按任务内容识别子代理，提问工具结构由 `tests/test_questions.py` 检查。`permissions` 同时按 61f44b3 的设计改为检查新会话继承最近一次明确选择的访问权限。
- 桌面测试 `framework`、`subagents` 同样改为按任务内容识别子代理；`subagents` 测试同时让 10 个子代理请求挂起，现在为测试连接设置 `model_request_limits.concurrency = 10`（默认每个连接 4 个并发请求，449a9ae 起），原先在默认值下必然死锁。`agent_tool` / `spawn_agent` 的并行数量上限改由 `tests/test_agent_schema_bounds.py` 检查。
- 完整桌面回归（74 项）中另外三项过时测试已修正：`resume` 删除强制自我介绍的断言（8e13e4c 起移除）；`moves` 的技能标签选择器跟随 09228c2 的非按钮标签，最终回复按回复区域查找（后台子代理完成后模型会再回答一次，先前的回答保留为过程说明），Windows 路径比较不区分大小写；`storage` 等待旧会话迁移（由存储进程在首次读取目录时完成）和面包屑渲染后再断言。三项在本轮之前的 HEAD 上同样失败。
- 代码结构：`engine_parts/loop.py`（约 1440 行）拆出 `sampling.py`、`usage.py`、`recovery.py`，方法代码原样搬移、行为不变；mypy 检查范围从 18 个文件扩大到 30 个（修正了 `hooks.py`、`vcr.py` 等处的类型注解）。
- 桌面测试 `models.e2e.cjs` 删除了检查强制自我介绍的过时断言（该行为已在 v0.1.41 前移除）。
- README 的安装方式改为从 GitHub 安装（`pip install "xuloom @ git+https://github.com/Dheudb/xuloom.git"`），直到 PyPI 发布可用。

- Windows 桌面包补齐固定版本、校验摘要的 ripgrep 与许可文件，文件搜索不再依赖开发机器的 PATH；库/源码保留系统 rg。新增缺失 PATH 的执行选择和构建校验回归。
- 四项桌面回归保留原有功能断言，统一隔离宿主凭据、预算、数据目录、代理和 ripgrep 配置，避免外部环境造成假失败或误用真实服务。

- 缺少系统 Git 时不再因 GitPython 导入失败而阻止普通对话；目录副本隔离仍可使用，Git 功能继续需要 Git。新增无 PATH 的启动回归。

## [0.1.45] - 2026-11-11

### 变更
- **不兼容**：PDF 读取改为可选依赖 `xuloom[pdf]`。PyMuPDF 采用 AGPL-3.0（或商业）许可，连同它引入的 onnxruntime、numpy 等约 200 MB；核心库不再默认安装它，MIT 核心保持宽松许可。没有安装时读取 PDF 会提示 `pip install "xuloom[pdf]"`。桌面发行包仍内置 PDF 支持。
- `xuloom --help` 列出全部子命令（`exec`、`serve`、`eval`、`plugins` 等，包括插件提供的命令）。
- 未配置模型时的提示改为分别说明 Python（`EngineConfig(...)`）、CLI（`OPENAI_API_KEY` / `OPENAI_MODEL` 等环境变量）、交互终端（`/model`）和桌面 App 的配置方式；CLI 文本输出不再附带面向桌面的中文提示行。

## [0.1.44] - 2026-11-11

### 新增
- 追踪：每个任务产生 `run` / `model` / `tool` span，子代理的 span 挂在父任务的同一个 trace 下。可通过 `EngineConfig(trace_exporters=...)`、插件的 `api.add_trace_exporter(...)`、`XULOOM_TRACE_FILE`（JSONL）或 `XULOOM_TRACE_OTLP=1`（OpenTelemetry/Langfuse）导出。见 [追踪与花费](docs/observability.md)。
- 花费：按 `~/.xuloom/pricing.json` 或 `EngineConfig(model_prices=...)` 中的价格计算每次请求的费用，结果出现在 `token_usage` 事件、`/status` 和会话记录里。
- 任务花费上限：`EngineConfig(max_cost_usd=...)` / `XULOOM_MAX_COST_USD`。子代理计入同一个任务；任务以 `cost_limit` 结束（CLI 退出码 8）。
- `xuloom.testing`：`ScriptedModel`（mock 模型）、`ReplayModel`（按顺序离线回放 `record_responses` / `XULOOM_RECORD_RESPONSES` 录下的响应）、`run_eval` 与 `xuloom eval`（独立工作区批量评测，失败时退出码 10）。见 [回放与评测](docs/replay-and-eval.md)。
- HTTP 服务模式 `xuloom serve` / `xuloom.server.XuloomServer`：会话、任务、SSE 事件流、远程审批与回答。默认只监听 localhost，需要 Bearer token，工作目录限定在 `--root` 内，不增加依赖。见 [HTTP 服务模式](docs/server.md)。
- 执行隔离新增 Linux bubblewrap 与 macOS seatbelt 后端（实验）：`--sandbox workspace` / `execution_mode="workspace"` 在三个平台上用法相同，后端不可用时明确失败。三个后端的工具进程都只接收白名单环境变量。见 [执行隔离](docs/queue-and-isolation.md#执行隔离)。
- 公开 API 新增 `Span`、`Tracer`、`TraceExporter`、`JsonlTraceExporter`、`ModelPrice`。
- `otel` extra；`dev` extra 加入 ruff、mypy、build、twine。
- CI：`ci.yml` 在 PR 上运行 ruff、mypy、测试与打包检查；`release.yml` 通过 PyPI Trusted Publishing 发布带标签的版本。
- `SECURITY.md`、Issue/PR 模板与本更新日志。

### 修复
- `.xuloom/hooks.json` 无法解析或某条 BeforeToolUse 规则出错时，工具调用现在会被拒绝，不再静默跳过规则；其他 Hook 事件的错误会记录 warning。
- 自动安全分类器出错时改为请求用户批准，不再视为“没有异议”。
- 超大工具结果写盘失败时保留原文，不再替换成指向不存在文件的预览（之前还可能因变量未赋值而崩溃）。
- `XULOOM_VCR_MODE=replay` 找不到录像或录像损坏时抛出 `ReplayMissError`，不再静默发出真实请求；显式设置的 VCR 模式不会被载入的旧会话覆盖；严格回放时，压缩和记忆提取请求也只从录像中取答复，不会发往模型。
- `record_responses` 录像现在也记录压缩和记忆提取请求（带 `purpose`），`ReplayModel` 按用途分别回放。之前回放时压缩会取走下一轮模型响应，导致后续轮次全部错位。
- 任务花费上限现在也覆盖上下文压缩和记忆提取请求（之前设为 $0 时仍会发出压缩请求），这些请求按模型计价；达到上限时在压缩前就停止。
- 当前任务的花费随会话保存，程序重启后恢复同一任务不再从 $0 开始计算。
- `xuloom serve`：客户端在模型生成期间断开 SSE（或阻塞请求）连接时，服务立即察觉并停止任务；发送响应头时连接就被重置的情况也会取消尚未开始的任务。

### 变更
- 旧的哈希录像 `XULOOM_VCR_MODE` 标为不推荐：行为不变，启用时提示一次，请改用 `record_responses` + `ReplayModel`。它跨会话难以命中，OpenAI 原生压缩也不会支持录制。
- **不兼容**：核心库与 CLI 最低要求 Python 3.10，不再支持 3.9；`ddgs` 成为唯一的搜索依赖（移除 `duckduckgo-search` 兼容路径）。
- 带日期的验证记录、旧的计划文档和 `releases/` 移到 `docs/history/`，原链接已同步更新。
- README 重写：先介绍框架和扩展方式，Scaling 后移并标注为实验功能。

## [0.1.43] - 2026-11-10

### 新增
- 媒体生成改为内置插件 `media`，只通过 `PluginAPI` 注册工具、CLI 命令和桌面 RPC。
- `PluginAPI.add_tool(latent=True)`、`add_cli_command`、`add_rpc_method(operation=, settings=)`；桌面端通过 `plugins.rpc` 发现插件方法，SSH 会话改为读取远端主机实际加载的插件列表。
- `plugins.json` 支持 `disabled` 列表，可以关闭内置插件；`EngineConfig` 新增 `bundled_plugins`、`disabled_plugins`。

### 修复
- `disabled_tools` 现在也覆盖插件工具。
- 单个插件的 CLI 命令配置失败时，不再导致所有 CLI 命令失败；未声明 `--json` 的插件命令自动获得该参数。
- 关闭 `media` 插件后，设置页不再显示“生成模型”标签页。

## [0.1.42] - 2026-11-10

### 新增
- `ModelAdapter` 模型适配层与注册表，内置 OpenAI 兼容和 Anthropic 适配器。
- 插件机制：`PluginAPI`、`load_plugins`、`~/.xuloom/plugins.json`、`xuloom plugins list|enable|disable`；插件注册整体生效或整体回退。
- 13 个生命周期 Hook 事件，支持 `engine.add_hook` 与 `hooks.json`。

## [0.1.41] - 2026-11-10

### 变更
- 引擎拆分为 `xuloom/core/engine_parts/` 下的多个 mixin，主循环拆成多个阶段方法。
- 新增不可变的 `EngineConfig` 和顶层公开 API（`from xuloom import ...`）。

[未发布]: https://github.com/Dheudb/xuloom/compare/v0.1.51...HEAD
[0.1.51]: https://github.com/Dheudb/xuloom/compare/v0.1.50...v0.1.51
[0.1.50]: https://github.com/Dheudb/xuloom/compare/v0.1.49...v0.1.50
[0.1.49]: https://github.com/Dheudb/xuloom/compare/v0.1.48...v0.1.49
[0.1.48]: https://github.com/Dheudb/xuloom/compare/v0.1.47...v0.1.48
[0.1.47]: https://github.com/Dheudb/xuloom/compare/v0.1.46...v0.1.47
[0.1.45]: https://github.com/Dheudb/xuloom/compare/v0.1.44...v0.1.45
[0.1.44]: https://github.com/Dheudb/xuloom/compare/v0.1.43...v0.1.44
[0.1.43]: https://github.com/Dheudb/xuloom/compare/v0.1.42...v0.1.43
[0.1.42]: https://github.com/Dheudb/xuloom/compare/v0.1.41...v0.1.42
[0.1.41]: https://github.com/Dheudb/xuloom/compare/v0.1.40...v0.1.41

[0.1.46]: https://github.com/Dheudb/xuloom/compare/v0.1.45...v0.1.46
