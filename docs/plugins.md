# 插件

插件把工具、Hook、模型适配器、Skills 和子代理打包在一起，供多个项目复用。

```python
# plugin.py
NAME = "my-plugin"

def register(api):
    api.add_tool(MyTool())                       # BeforeToolUse/权限/回执照常适用
    api.add_tool(RareTool(), latent=True)        # 隐藏，模型通过 tool_search_tool 找到后再启用
    api.add_hook("AfterToolUse", on_edit, tools=["file_edit_tool"])
    api.add_model_adapter(MyAdapter())           # 进程级注册
    api.add_skills(Path(__file__).parent / "skills")
    api.add_agents(Path(__file__).parent / "agents")
    api.add_cli_command("my-cmd", configure, handler, help="...")   # xuloom my-cmd ...
    api.add_rpc_method("my-plugin.status", rpc_status)              # 桌面端调用
    api.add_trace_exporter(MyExporter())                            # 接收 run/model/tool span
```

| 方法 | 说明 |
| --- | --- |
| `add_tool(tool, latent=False)` | 可信 Python 工具；`latent=True` 不进入每次请求，降低提示开销 |
| `add_hook(event, callback, tools=, modes=)` | 见 [生命周期 Hook](hooks.md) |
| `add_model_adapter(adapter)` | 见 [模型适配器](model-adapters.md) |
| `add_skills(dir)` / `add_agents(dir)` | 提供 Skill 与子代理定义目录 |
| `add_cli_command(name, configure, handler, help=)` | `configure(parser)` 添加 argparse 参数；`async handler(args, emit)` 返回字典，`text` 在非 `--json` 模式下打印，`ok`/`reason` 决定退出码。不能覆盖内置命令 |
| `add_trace_exporter(exporter)` | 接收每个结束的 span（`exporter.export(span)`），见 [追踪与花费](observability.md)。子代理共享父引擎的 tracer，不会重复收到 |
| `add_rpc_method("前缀.动作", handler)` | 桌面端 RPC。`async handler(params, context)`，`context` 是 `RpcContext`：`cwd`、`session_id`、`require_idle()`、`await engine(write=)`、`emit(event)`、`start_operation(name, work)`。不能覆盖内置方法 |

插件可以是带 `register(api)` 的对象、模块或函数。每个加载它的引擎都会调用一次 `register`，因此应在其中创建新的工具实例，不要做耗时或有副作用的操作。

完整示例：[examples/plugins/ollama-native](../examples/plugins/ollama-native)（Ollama 原生适配器、一个工具、一个 Hook 和一个 Skill）。

## 内置插件

Xuloom 自带的插件列在 `xuloom.plugins.BUNDLED_PLUGINS`，默认开启。目前是 `media`（媒体生成）：它的工具、`xuloom media` 命令和桌面端 `media.*` 接口全部通过上面的公开 API 注册，核心代码不直接引用它，可作为编写插件的参考。

- 用户：`xuloom plugins disable media` / `enable media`，记录在 `plugins.json` 的 `disabled` 列表。
- 程序宿主：默认加载内置插件，不读取用户的 `plugins.json`；用 `EngineConfig(bundled_plugins=False)` 或 `disabled_plugins={"media"}` 关闭。
- 子代理只继承父引擎实际加载的插件。

## 安装与启用

插件不会因为存在就自动运行，必须显式启用：

| 方式 | 来源 | 启用 |
| --- | --- | --- |
| Python 包 | `entry_points` 组 `xuloom.plugins`，例如 `[project.entry-points."xuloom.plugins"] my-plugin = "my_pkg.plugin"` | `xuloom plugins enable my-plugin` |
| 目录 | `~/.xuloom/plugins/<名称>/plugin.py` | `xuloom plugins enable <名称>` |
| 程序 | 任意对象 | `EngineConfig(plugins=[obj, "已安装名称"])` |

`xuloom plugins list` 显示内置和已安装插件及启用状态。设置保存在 `~/.xuloom/plugins.json`（`enabled`：启用的已安装插件；`disabled`：关闭的内置插件），对新会话生效。CLI 与桌面 App 读取它；程序宿主需要设置 `EngineConfig(load_enabled_plugins=True)` 才会读取。

## 安全与失败处理

- 插件代码与 Xuloom 同权运行，只安装可信来源的插件。
- 工作区内的 `.xuloom/plugins` 不会被加载，打开别人的仓库不会执行其中的插件。
- 启用列表中的插件加载失败时，引擎照常启动，错误记录在 `engine.plugin_errors` 并写入日志；`EngineConfig(plugins=...)` 显式传入的插件失败会直接抛出 `PluginError`。
- 子代理继承父引擎已加载的插件，不重新读取启用列表。
- 插件工具名不能与内置工具或其他工具重复。
- 如果当前会话使用的模型适配器正来自加载失败的插件，引擎不会启动（抛出 `PluginError`），不会在缺少该插件其余部分（例如保护 Hook）的情况下调用模型。
- 注册是整体生效的：`register` 抛出异常，或其中任一工具、Hook 无法安装时，这个插件的工具、Hook、Skill/子代理目录和模型适配器全部撤回，不会留下一部分。
