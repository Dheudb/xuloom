# 模型适配器

`ModelAdapter` 把 QueryEngine 接到任意模型 API。内置两个原生适配器：

| 名称 | 协议 | 说明 |
| --- | --- | --- |
| `openai` | OpenAI Chat Completions（兼容接口）/ 官方模型的 Responses | 原生流式路径，模型仍在输出时即可开始执行只读工具 |
| `anthropic` | Anthropic Messages | 原生流式路径，支持提示缓存与思考配置 |

其他 API（Ollama 原生接口、Gemini、Bedrock、自建网关等）通过注册自定义适配器接入，选择方式与内置协议相同：`EngineConfig(model_provider="<适配器名>")`。未注册的名称会直接报错，不会退回到其他协议。

## 最小示例

```python
from xuloom import EngineConfig, QueryEngine
from xuloom.models import ModelAdapter, ToolCall, model_response, register_adapter

class MyAdapter(ModelAdapter):
    name = "my-api"
    default_base_url = "https://api.example.com"

    async def stream_turn(self, request):
        body = {"model": request.model, "messages": request.openai_messages(),
                "tools": request.openai_tools(), "max_tokens": request.max_tokens}
        reply = (await request.client.http.post(request.client.url("/v1/chat"), json=body)).json()
        calls = [ToolCall(c["id"], c["name"], c["arguments"]) for c in reply.get("tool_calls", [])]
        return model_response(text=reply.get("text", ""), tool_calls=calls,
                              input_tokens=reply["usage"]["input"], output_tokens=reply["usage"]["output"])

register_adapter(MyAdapter())
engine = QueryEngine(EngineConfig(model="m1", model_provider="my-api", api_key="..."))
```

完整可运行示例（含工具调用格式转换）见 [examples/plugins/ollama-native/plugin.py](../examples/plugins/ollama-native/plugin.py)。

## 约定

| 成员 | 必需 | 说明 |
| --- | --- | --- |
| `name` | 是 | 小写，`model_provider` 使用的名称。`openai`、`anthropic` 不能被覆盖 |
| `stream_turn(request)` | 是 | 执行一轮请求，返回 `model_response(...)` |
| `create_client(...)` | 否 | 默认返回 `HttpModelClient`：`client.http` 是已应用代理模式与连接限流的 `httpx.AsyncClient`，`client.url(path)` 拼接地址，`client.api_key` 为已校验的 Key。自定义客户端需提供 `async close()` 与 `is_closed` |
| `complete_text(...)` | 否 | 上下文压缩和记忆提取使用的纯文本补全；默认复用 `stream_turn` |
| `is_transient_error(e)` | 否 | 返回真时切换到 fallback 模型重试本轮；默认识别 429 与 5xx |
| `is_context_overflow_error(e)` | 否 | 返回真时引擎压缩上下文后重试 |

`ModelRequest` 提供 `model`、`system_prompt`、`messages`（规范格式，见下）、`tools`、`max_tokens`、`thinking`、`reasoning_effort`、`turn` 和 `client`，以及：

- `openai_messages()` / `openai_tools()`：转换为 OpenAI Chat 格式；
- `await emit_text(text)`：边收边显示文字（可选；未调用时引擎在结束后一次性显示）；
- `await emit_status("connected" | "reasoning" | "receiving")`：更新界面状态（可选）。

规范消息格式与 Anthropic Messages 相同：`text`、`image`、`tool_use`（`id`、`name`、`input`）和 `tool_result`（`tool_use_id`、`content`、`is_error`）块。

## 行为边界

- 自定义适配器返回后，引擎才执行工具调用；权限检查、BeforeToolUse/AfterToolUse Hook、回执与持久化与内置协议相同。
- 用户停止任务、或调用方取消 `engine.run()` 时，正在进行的 `stream_turn` 会被取消，并在返回前等待它结束。
- 每个客户端固定使用创建它的适配器实例；之后重新注册同名适配器（例如另一个引擎再次加载同一插件）只影响新建的客户端。
- 返回值不是 `model_response(...)` 时，本轮以 `api_error` 结束，已有记录保留。
- 提示缓存、原生思考摘要、Responses 原生压缩只属于内置适配器。
- 桌面设置页目前只列出内置协议；自定义适配器通过插件或程序接入使用。
