# 个人开发者扩展接口

当前公开接入面复用 QueryEngine，不另建执行器或插件管理器：

公开名称统一从包顶层导入：`from xuloom import QueryEngine, EngineConfig, BaseAgenticTool, RunResult, ApprovalPolicy`。

| 接口 | 约定 |
| --- | --- |
| `EngineConfig(...)` | 不可变配置对象，包含模型连接、推理、工作区、权限、协作模式、`extra_tools`、`disabled_tools`。`replace(**changes)` 生成副本，未知字段抛 `TypeError`；`resolved()` 是唯一读取环境变量默认值的地方；`to_public_dict()` 不含密钥 |
| `QueryEngine(config, **options)` / `async with` / `aclose()` | 实例绑定模型、连接、工作目录；关键字参数覆盖 `config` 字段，结果见 `engine.config`；关闭释放所属资源和写者锁 |
| `register_tool(BaseAgenticTool)` | 空闲时注册可信 Python 工具；验证定义与名称，禁止覆盖已有工具，仍经过参数校验和权限入口 |
| `set_event_handler(callback)` | 一个同步能力事件监听器；接收 `tool_started`、`tool_result`、worker 等既有事件。快速返回；异步回调不接受，可用 `queue.put_nowait` 转交。监听器异常记录类型而不改变工具回执；传 `None` 移除 |
| `submit_message(...)` | 异步读取文本、模型状态等流事件；唯一 `final` 的 `result` 为真实回执。提前关闭消费者会取消所属执行并保存取消结果 |
| `run(...)` | 非流式执行，返回 `RunResult`；文本不是完成状态，以 `ok/reason/persisted` 判断 |
| `request_abort()` | 请求停止；已完成结果保留，未确认副作用不会因此变成可安全重试 |
| `queue_input(text, request_id=None)` | 同一事件循环内向运行中主任务追加文字；先保存再确认，安全边界送达。返回 `id/text/status`，详见[运行中追加指令](live-input.md) |
| `load_session(id)` + `submit_message('', resume=True)` | 显式恢复原任务；不替换原 prompt，不通过猜测重放未知操作 |
| `add_hook(event, callback, tools=None, modes=None)` / `remove_hook(handle)` | Python 生命周期 Hook，事件与返回值见 [生命周期 Hook](hooks.md) |
| `EngineConfig(plugins=[...], load_enabled_plugins=...)` / `engine.plugin_errors` | 加载插件并查看失败原因，见 [插件](plugins.md) |
| `xuloom.models.register_adapter(adapter)` | 注册自定义模型 API，见 [模型适配器](model-adapters.md) |
| `agents(action, **parameters)` | 原生 worker 的结构化启动、查询、消息、等待与取消；具体字段见 [框架接口](framework.md) |

工具定义继承 `xuloom.BaseAgenticTool`，输入使用 Pydantic 2 模型。工具是用户信任的本地 Python 代码，与宿主进程同权；`is_read_only` 等声明必须真实，不能把它们当作 OS 隔离保证。运行时工具回执的状态和值沿用现有协议；工具执行成功和 worker 的 `completed` 是不同层次。

完整示例：[examples/extension_project/app.py](../examples/extension_project/app.py)。可以复制到仓库外，安装 Xuloom 后运行：

```powershell
$env:EXAMPLE_API_KEY = '填写自己的 Key'
python app.py --base-url '填写兼容 API 根地址' --model '填写模型名'
python app.py --base-url '填写兼容 API 根地址' --model '填写模型名' --session 会话ID --resume
```

示例验证了自定义工具、模型端点、流/能力事件、取消与重启恢复。测试使用真实 SDK 与回环 HTTP，不验证远端模型质量。其他模型 API 通过 [模型适配器](model-adapters.md) 接入；SDK 对象替换和 `create_model_client` 仍属内部实现。

公开参数、回执字段和事件采用增量兼容：调用方忽略未知的可选字段和事件类型，以 `reason` / `persisted` 等语义字段判断；破坏性更改应在版本说明中给出迁移方式。`_` 前缀方法、`available_tools` 字典和 `tool_context` 的直接修改仍为内部实现，不作为稳定扩展契约。需要使用能力事件的旧宿主可以改用 `set_event_handler`，不用更换执行协议。

## 构造方式

```python
from xuloom import EngineConfig, QueryEngine

config = EngineConfig(model="my-model", model_provider="openai", api_base_url="https://example/v1",
                      api_key="...", cwd="/work", extra_tools=[MyTool()], disabled_tools={"repl_tool"})
async with QueryEngine(config) as engine:
    result = await engine.run("任务")
```

`QueryEngine(model=..., cwd=...)` 与上面等价。`disabled_tools` 移除的内置工具不会出现在模型工具列表中，自定义工具可以使用该名称。`xuloom.default_tools()` 返回内置工具的新实例，可用于检查名称。
