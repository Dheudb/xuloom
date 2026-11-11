# 回放、mock 模型与评测

`xuloom.testing` 提供三样工具，都不需要真实模型或 API Key：

| 工具 | 用途 |
| --- | --- |
| `ScriptedModel` | 按脚本返回模型响应的 mock 适配器，用于单元测试 |
| `record_responses` + `ReplayModel` | 录下一次真实运行的模型响应，之后离线、零费用地重放 |
| `run_eval` / `xuloom eval` | 在独立工作区里批量运行任务，并按检查项判断通过与否 |

## Mock 模型

```python
from xuloom import ApprovalPolicy, EngineConfig, QueryEngine
from xuloom.models import ToolCall, model_response
from xuloom.testing import ScriptedModel

model = ScriptedModel([
    model_response(tool_calls=[ToolCall("c1", "file_write_tool", {"file_path": "a.txt", "content": "hi"})]),
    model_response(text="done"),
])
with model:                                    # 注册适配器，退出时移除
    config = model.config(EngineConfig(cwd=tmp, approval_policy=ApprovalPolicy("full")))
    async with QueryEngine(config) as engine:
        result = await engine.run("write a.txt")
assert result.ok and len(model.requests) == 2  # 每次请求的 system prompt、消息和工具都可以断言
```

脚本中的每一步可以是 `model_response(...)`、要抛出的异常，或 `step(request)` 函数（同步或异步）。脚本用完后，再收到请求会抛出 `ScriptExhausted`，任务以失败结束，不会挂起；也可以传 `default=` 作为兜底响应。

## 录制与回放

```bash
XULOOM_RECORD_RESPONSES=run.jsonl xuloom exec "整理 docs 目录" --permission full   # 录制（也可以用 EngineConfig(record_responses=...)）
```

```python
from xuloom.testing import ReplayModel

with ReplayModel("run.jsonl") as replay:
    async with QueryEngine(replay.config(base_config)) as engine:
        result = await engine.run("整理 docs 目录")
```

- 录像是 JSONL，每行是一次模型响应，按顺序回放，不按请求哈希匹配。system prompt、路径或日期变化后，录像仍然可用。
- 上下文压缩和记忆提取请求也会录下，记录行带 `purpose`（如 `compaction`、`session_memory`）和返回的 `text`。回放时它们按用途各自取用，不会占用模型轮次的响应，长任务中途压缩后仍能对齐回放。录像里没有对应用途的记录时以 `ScriptExhausted` 失败。原生压缩（OpenAI Responses `/responses/compact`）不录制。
- 回放时工具会真实执行，因此回放检验的是 harness 本身：工具、权限、Hook、持久化和事件流。它不检验模型。
- 请求次数超过录像中的响应数时以 `ScriptExhausted` 失败，不会转去调用真实模型。
- 只录制顶层引擎。回放请使用不委派子代理的任务（`collaboration_mode="solo"`，评测会自动设置）。

## 旧的哈希录像（不推荐）

`XULOOM_VCR_MODE=record|replay|replay_record` 是早期的哈希匹配录像，已被上面的 `record_responses` + `ReplayModel` 取代，只为兼容保留，不再增加功能；启用时会在日志中提示一次。新用法请一律使用 `ReplayModel`。

它的现有行为：现在 `replay` 模式找不到录像或录像损坏时，会抛出 `ReplayMissError`，不再静默转为真实请求。通过 `XULOOM_VCR_MODE` 或构造参数显式指定的模式优先于会话中保存的模式，载入旧会话不会把它改回 `off`。压缩和记忆提取请求按各自的内容录制和回放，严格回放时找不到录像同样以 `ReplayMissError` 失败，不会发往模型；OpenAI 原生压缩（`/responses/compact`）不录制，严格回放时直接拒绝，以后也不计划支持。

注意：哈希录像要求请求逐字节相同，而 system prompt 包含会话相关的内容，新会话的请求通常无法命中旧录像。需要跨会话、跨机器回放时，请使用上面的 `ReplayModel`。

## 评测

```json
[
  {"id": "write-file",
   "prompt": "创建 hello.txt，内容为 hi",
   "files": {"README.md": "已有文件"},
   "checks": [{"type": "ok"},
              {"type": "file_contains", "path": "hello.txt", "text": "hi"},
              {"type": "tool_called", "name": "file_write_tool"},
              {"type": "max_cost_usd", "value": 0.05}]},
  {"id": "offline", "prompt": "整理 docs 目录", "cassette": "cassettes/run.jsonl",
   "checks": [{"type": "ok"}, {"type": "max_requests", "value": 6}]}
]
```

```bash
xuloom eval cases.json --permission full           # 人类可读的 PASS/FAIL 列表
xuloom eval cases.json --json --only write-file    # JSON 进度事件和最终报告
```

- 每个用例在新建的空工作区中运行，`files` 先写入这个工作区。用例默认放在临时目录，运行结束后删除；`--workdir` 可以保留工作区以便检查。
- 带 `cassette` 的用例离线回放，其余用例使用已配置的模型连接（与 `xuloom exec` 相同）。
- `--permission ask`（默认）会拒绝所有需要审批的操作；需要执行工具时请用 `full`，并确认用例来源可信。
- 全部通过时退出码为 0，有用例失败时为 10。

| 检查 | 字段 | 判断 |
| --- | --- | --- |
| `ok` | | 任务完成（`RunResult.ok`） |
| `reason` | `equals` | 结束原因等于给定值，例如 `cost_limit` |
| `contains` / `not_contains` | `text`、`ignore_case` | 最终回答包含 / 不包含该文本 |
| `regex` | `pattern` | 最终回答匹配正则（多行模式） |
| `file_exists` / `file_missing` | `path` | 工作区内的文件存在 / 不存在 |
| `file_contains` | `path`、`text` | 工作区内的文件包含该文本 |
| `tool_called` / `tool_not_called` | `name` | 模型调用过 / 没有调用过该工具 |
| `max_cost_usd` | `value` | 任务费用不超过该值；没有配置价格时判为失败，见[追踪与花费](observability.md) |
| `max_requests` / `max_seconds` | `value` | 模型请求次数 / 耗时不超过该值 |

路径不能离开用例工作区。程序接口 `run_eval(cases, config, workdir=None, permission_handler=None, on_event=None)` 返回 `EvalReport`，`report.to_dict()` 的内容与 CLI 的 JSON 报告相同。
