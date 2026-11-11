# 追踪与花费

每个任务产生一组 span，交给可替换的导出器；每次模型请求按配置的价格计费，并可为单个任务设置花费上限。

## Span

| kind | 名称 | 主要属性 |
| --- | --- | --- |
| `run` | `xuloom.run` | `xuloom.session_id`、`xuloom.agent_id`、`xuloom.agent_depth`、`xuloom.result.reason`、`xuloom.task.cost_usd`、`xuloom.session.cost_usd` |
| `model` | `xuloom.model` | `gen_ai.request.model`、`gen_ai.usage.input_tokens` / `output_tokens` / `cache_read_input_tokens` / `cache_creation_input_tokens`、`gen_ai.response.finish_reasons`、`xuloom.cost_usd`、`xuloom.usage.source`（`api_usage` 或本地估算）、`xuloom.replayed`（旧的哈希录像回放） |
| `tool` | `xuloom.tool` | `gen_ai.tool.name`、`gen_ai.tool.call.id`；工具返回错误时状态为 `error` |

- 每个 span 带 `start_time` / `end_time`（epoch 秒）和 `duration_ms`。异常、取消以及非 `completed` 的结束原因记为 `status="error"`，`error` 字段写明原因。
- 模型和工具 span 是所在 `run` 的子 span。子代理共享父引擎的 tracer，它的 `run` 挂在启动它的 `agent_tool` span 下，因此整次委派属于同一个 trace。
- 属性名尽量沿用 OpenTelemetry GenAI 约定，所以 JSONL 文件和 OTel 后端里的字段相同。
- 导出器抛出的异常只记录一次警告，不会影响任务。
- 压缩摘要、原生压缩和记忆提取等辅助请求目前不产生 span，但会按所用模型计价，计入任务与会话费用（`engine.auxiliary_usage[用途]["cost_usd"]`），并受任务花费上限约束。

## 导出器

```python
from xuloom import EngineConfig, JsonlTraceExporter
from xuloom.observability import OpenTelemetryTraceExporter

config = EngineConfig(trace_exporters=(JsonlTraceExporter("trace.jsonl"), OpenTelemetryTraceExporter()))
```

导出器是任意带 `export(span)` 方法的对象；`on_start(span)` 和 `shutdown()` 可选，`shutdown()` 在引擎关闭时调用。`span.to_dict()` 给出可序列化的内容。

| 方式 | 说明 |
| --- | --- |
| `EngineConfig(trace_exporters=(...))` | 宿主程序传入 |
| `api.add_trace_exporter(exporter)` | 插件提供，见 [插件](plugins.md) |
| `XULOOM_TRACE_FILE=path.jsonl` | 每个结束的 span 追加一行 JSON；CLI 和桌面端也适用 |
| `XULOOM_TRACE_OTLP=1` | 用 OTLP/HTTP 发送到标准 `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_EXPORTER_OTLP_HEADERS`。需要 `pip install xuloom[otel]` |

`OpenTelemetryTraceExporter(tracer_provider=None)` 默认使用宿主程序已经设置的全局 provider，并保持父子结构。Langfuse、Jaeger、Grafana Tempo 等支持 OTLP 的后端都可以通过它接入。以 Langfuse 为例：

```bash
export XULOOM_TRACE_OTLP=1
export OTEL_EXPORTER_OTLP_ENDPOINT=https://cloud.langfuse.com/api/public/otel
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic <base64(public_key:secret_key)>"
```

## 价格

Xuloom 不内置价格表（价格经常变化，不同服务商也不一样），需要按自己使用的模型配置，单位是每百万 token 的美元价格：

```json
// ~/.xuloom/pricing.json
{"models": {
  "claude-sonnet-*": {"input": 3, "output": 15, "cache_write": 3.75, "cache_read": 0.3},
  "gpt-4.1": {"input": 2, "output": 8}
}}
```

- `EngineConfig(model_prices={...})` 的同名条目优先于文件。
- 文件还可以写 `"max_cost_usd": 0.5`，作为没有通过 `EngineConfig` 或 `XULOOM_MAX_COST_USD` 设置时的任务上限。桌面端 **设置 → 费用** 编辑的就是这个文件。
- 键可以是精确模型 ID，也可以是 `fnmatch` 通配符；精确 ID 优先。
- `input_tokens` 不含缓存读写，缓存读写按 `cache_read` / `cache_write` 单独计价。这两项缺省时按 `input` 价格计算，只会高估，不会低估。
- OpenAI 兼容接口返回的 `prompt_tokens` 已包含缓存命中，因此按全价计算。
- 文件格式错误时记录警告，并当作没有价格。

已计价的费用会出现在：

- `token_usage` 事件的 `cost_usd`、`session_cost_usd`、`task_cost_usd`；
- `engine.token_usage_history[*]["cost_usd"]`（没有价格时为 `None`）；
- `engine.session_cost`（随会话保存，`unpriced_requests` 统计未计价的请求数）；
- `/status` 输出和 span 属性。

从录像回放的响应（`ReplayModel` 或旧的哈希录像）费用为 0。

## 任务花费上限

上限是**停止条件，不是账单保证**：达到上限后不再发出新的请求，但无法让最终账单绝不超过设定值。

```python
EngineConfig(max_cost_usd=0.50)   # 或环境变量 XULOOM_MAX_COST_USD=0.50
```

- 上限按“任务”计算：顶层引擎每开始一个新任务就清零。当前任务的花费随会话保存，程序重启后恢复（resume）同一任务时继续累计，不会清零。
- 子代理共享父任务的计数器和上限。
- 每次模型请求之前检查。已达到上限、即将调用的模型没有价格，或本任务已有未计价的请求时，任务以 `cost_limit` 结束（CLI 退出码 8），不会发出这次请求。设了上限却无法计价时一律拒绝，避免花费不受控制。
- 压缩和记忆提取在发出每个请求前也做同样的检查；任务在压缩前就已达到上限时，直接以 `cost_limit` 结束，不再压缩。
- 已经发出的请求不会被中断。最后一次请求、并行子代理同时在途的请求，都会完成并计费，所以最终花费可能超过上限；并行越多，可能超出越多。
- 费用按 `pricing.json` 的价格计算；服务端没有返回用量时按估算 token 计。实际金额以供应商账单为准，需要硬性额度时请同时在供应商后台设置。
