# 生命周期 Hook

Hook 在任务的固定节点运行，可以观察、为模型补充上下文，部分事件还能拦截或改写操作。有两种写法，可以同时使用：

- **配置文件**：`<工作区>/.xuloom/hooks.json`，模板或外部命令；
- **Python 回调**：`engine.add_hook(event, callback)`，或在[插件](plugins.md)里 `api.add_hook(...)`。

## 事件

| 事件 | 时机 | 除观察和补充上下文外还能 |
| --- | --- | --- |
| `SessionStart` | 会话第一次运行、首个模型请求之前 | — |
| `UserPromptSubmit` | 新任务即将发送 | `block`：拦截，本次以 `hook_blocked` 结束，不调用模型 |
| `BeforeToolUse` | 工具执行前 | `reject`：拒绝执行；`updated_input`：改写参数 |
| `AfterToolUse` | 工具结果保存后 | 补充的上下文出现在下一次模型请求中 |
| `PostSampling` | 一轮最终回答生成后（旧名 `PostSamplingHook`） | — |
| `Stop` | 接受最终回答前（旧名 `StopHook`） | `block` / `retry`：附带原因让模型继续 |
| `TurnEnd` | 一轮成功结束 | — |
| `TurnError` | 一轮出错或被取消 | — |
| `PreCompact` / `PostCompact` | 上下文压缩前 / 压缩改变历史后 | — |
| `SubagentStart` / `SubagentStop` | 子代理记录创建 / 进入结束状态 | — |
| `SessionEnd` | 引擎关闭 | — |

所有事件的载荷都包含 `event`、`session_id`、`agent_id`、`agent_role`、`cwd`、`mode`，另有事件自己的字段，例如 `prompt`、`tool_name`、`tool_input`、`status`、`final_answer`、`error_type`、`subagent`。

## Python 回调

```python
def guard(payload):
    if payload["tool_input"].get("command", "").startswith("rm -rf"):
        return {"decision": "reject", "reason": "不允许递归删除"}

engine.add_hook("BeforeToolUse", guard, tools=["bash_tool"])
engine.add_hook("AfterToolUse", lambda p: {"context": "修改文件后请运行测试"}, tools=["file_edit_tool"])
handle = engine.add_hook("Stop", check_answer)   # 可以是 async 函数
engine.remove_hook(handle)
```

回调接收载荷的副本，返回 `None`（只观察）或字典：`decision`、`reason`、`context`（作为系统提醒加入对话）、`updated_input`（仅 BeforeToolUse）。可用 `tools=[...]`、`modes=[...]` 过滤。

`context` 的去向：`SessionStart`、`UserPromptSubmit`、`TurnEnd` 立即加入对话；`BeforeToolUse` 附在该工具结果前；`PostSampling` 记为采样后提醒；其余事件（`AfterToolUse`、`Stop`、`TurnError`、`PreCompact`、`PostCompact`、`SubagentStart`、`SubagentStop`）进入同一个队列，在本会话的下一次模型请求前加入，例如出错重试的那次请求。`SessionEnd` 的 `context` 不再使用。

回调抛出异常时只记录异常类型，不中断任务；**BeforeToolUse 例外**：回调出错会拒绝这次工具调用，避免安全检查失效后工具照常执行。

## 配置文件

```json
{
  "UserPromptSubmit": [{"content": "本项目使用 pnpm，不要使用 npm。"}],
  "BeforeToolUse": [{"tools": ["bash_tool"], "behavior": "reject", "content": "此工作区禁止命令执行"}],
  "AfterToolUse": [{"type": "command", "command": "python scripts/lint_changed.py", "tools": ["file_edit_tool"]}],
  "Stop": [{"type": "command", "command": "pytest -q"}]
}
```

- 模板 hook：`content` 支持 `{字段名}` 占位；`behavior` 取该事件支持的决定。
- 命令 hook：载荷以 JSON 从标准输入传入，工作目录为工作区。标准输出第一行若是 JSON，读取 `decision`、`reason`、`context`；退出码 2 表示 `block`。超时默认 120 秒。系统执行隔离开启时命令 hook 不执行。
- `StopHook`、`PostSamplingHook` 等旧键继续有效，与新名称合并执行。
