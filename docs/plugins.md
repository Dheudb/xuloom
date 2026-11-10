# 插件

插件把工具、Hook、模型适配器、Skills 和子代理打包在一起，供多个项目复用。

```python
# plugin.py
NAME = "my-plugin"

def register(api):
    api.add_tool(MyTool())                       # BeforeToolUse/权限/回执照常适用
    api.add_hook("AfterToolUse", on_edit, tools=["file_edit_tool"])
    api.add_model_adapter(MyAdapter())           # 进程级注册
    api.add_skills(Path(__file__).parent / "skills")
    api.add_agents(Path(__file__).parent / "agents")
```

插件可以是带 `register(api)` 的对象、模块或函数。每个加载它的引擎都会调用一次 `register`，因此应在其中创建新的工具实例，不要做耗时或有副作用的操作。

完整示例：[examples/plugins/ollama-native](../examples/plugins/ollama-native/)（Ollama 原生适配器、一个工具、一个 Hook 和一个 Skill）。

## 安装与启用

插件不会因为存在就自动运行，必须显式启用：

| 方式 | 来源 | 启用 |
| --- | --- | --- |
| Python 包 | `entry_points` 组 `xuloom.plugins`，例如 `[project.entry-points."xuloom.plugins"] my-plugin = "my_pkg.plugin"` | `xuloom plugins enable my-plugin` |
| 目录 | `~/.xuloom/plugins/<名称>/plugin.py` | `xuloom plugins enable <名称>` |
| 程序 | 任意对象 | `EngineConfig(plugins=[obj, "已安装名称"])` |

`xuloom plugins list` 显示已安装插件和启用状态。启用列表保存在 `~/.xuloom/plugins.json`，对新会话生效。CLI 与桌面 App 加载启用列表；程序宿主需要设置 `EngineConfig(load_enabled_plugins=True)` 才会读取它。

## 安全与失败处理

- 插件代码与 Xuloom 同权运行，只安装可信来源的插件。
- 工作区内的 `.xuloom/plugins` 不会被加载，打开别人的仓库不会执行其中的插件。
- 启用列表中的插件加载失败时，引擎照常启动，错误记录在 `engine.plugin_errors` 并写入日志；`EngineConfig(plugins=...)` 显式传入的插件失败会直接抛出 `PluginError`。
- 子代理继承父引擎已加载的插件，不重新读取启用列表。
- 插件工具名不能与内置工具或其他工具重复。
- 如果当前会话使用的模型适配器正来自加载失败的插件，引擎不会启动（抛出 `PluginError`），不会在缺少该插件其余部分（例如保护 Hook）的情况下调用模型。
- 注册是整体生效的：`register` 抛出异常，或其中任一工具、Hook 无法安装时，这个插件的工具、Hook、Skill/子代理目录和模型适配器全部撤回，不会留下一部分。
