# 更新日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。1.0 之前的次版本可能包含不兼容变更，均在下面注明。v0.1.40 及更早版本的说明见 [GitHub Releases](https://github.com/Dheudb/xuloom/releases) 与 [历史记录](docs/history/README.md)。

## [未发布]

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

[未发布]: https://github.com/Dheudb/xuloom/compare/v0.1.44...HEAD
[0.1.44]: https://github.com/Dheudb/xuloom/compare/v0.1.43...v0.1.44
[0.1.43]: https://github.com/Dheudb/xuloom/compare/v0.1.42...v0.1.43
[0.1.42]: https://github.com/Dheudb/xuloom/compare/v0.1.41...v0.1.42
[0.1.41]: https://github.com/Dheudb/xuloom/compare/v0.1.40...v0.1.41
