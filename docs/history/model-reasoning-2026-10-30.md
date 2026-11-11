# 模型思考控制与原生派发入口（2026-10-30）

## 桌面使用

在 **设置 → 模型连接** 选择 API 和模型 ID，在“当前模型的思考能力”中声明该接口使用的参数格式、可用强度，以及是否允许关闭 Thinking，然后保存。每个连接、每个模型分别保存，不能通过网关别名可靠推断能力；编辑器中的预设是配置起点，不是远端验证结果。

输入框右下角会显示该模型可用的 Thinking 与推理强度选择器。Thinking 只显示“开启思考／关闭思考”，旧 `default` 思考配置兼容为开启；未声明支持关闭的模型禁用关闭选项。推理强度仍可选择接口默认值。关闭 Thinking 时不发送冲突的 effort，先前选择的强度保留，重新开启后可继续使用。切换到未声明的模型会隐藏这些控件并清除原模型的参数；不静默切换模型，也不通过追加 `/think` 或 `/no_think` 提示词来模拟参数。

支持以下显式格式：

| 设置格式 | 请求参数 |
| --- | --- |
| OpenAI | `reasoning_effort`；模型支持时可声明 `none` |
| DeepSeek 兼容接口 | `thinking.type` 和 `reasoning_effort` |
| Qwen 兼容接口 | `enable_thinking`；仅显式声明时附加强度 |
| Qwen 本地服务 | `chat_template_kwargs.enable_thinking`；须由该服务支持 |
| Anthropic 自适应 | `thinking.type=adaptive/disabled` 与 `output_config.effort` |
| Anthropic 固定 Thinking 预算 | `thinking.type=enabled` 与 `budget_tokens`，预算小于该次响应上限 |

这些参数只影响模型推理，与 **Solo / Auto / Ultra**、工具审批和工作区隔离独立。部分模型始终思考，应取消“模型支持关闭 Thinking”。部分模型的 effort 有额外组合限制，应按供应商文档声明，而不是勾选所有档位。Anthropic 的固定预算是模型自身协议要求，并非目标模式的总输出预算。

## 配置与 CLI

连接配置中的 `model_reasoning` 按模型 ID 保存，例如：

```json
{
  "model_reasoning": {
    "your-model-id": {
      "adapter": "deepseek",
      "efforts": ["low", "high", "max"],
      "canDisable": true,
      "thinking": "enabled",
      "effort": "default",
      "budgetTokens": 2048
    }
  }
}
```

UI RPC 使用驼峰字段 `modelReasoning`，磁盘使用 `model_reasoning`。这份示例不包含 URL 或凭据。修改任何连接时仍走原配置事务和锁；运行中的桌面任务不允许更改推理设置。

CLI 会读取相同的连接配置。非交互调用可使用 `xuloom exec --effort high --thinking enabled "任务"`；交互端使用 `/thinking` 查看，`/thinking disabled` 切换开关，`/thinking effort high` 切换强度。CLI 的临时选择保存在本会话，不改写全局连接默认值。未声明 Thinking 格式时拒绝猜测；OpenAI 兼容 CLI 仍保留显式 effort 的原程序接口。恢复会话时只在当前声明的能力范围内恢复选择。

同模型子代理继承父任务的实际选择，包含 CLI 临时覆盖；改用其他模型时不继承不兼容的参数。显式子代理 effort 超出已声明能力时，在创建 worker 前报错。重放记录的请求身份包含推理设置，避免用不同设置命中同一份响应。

## Ultra 与 Codex 对照

固定检查的 Codex 提交为 [`c9750d7`](https://github.com/openai/codex/tree/c9750d7f50052b29f53def02b5cf2160ae49a3d9)。[`multi_agent_mode_instructions.rs`](https://github.com/openai/codex/blob/c9750d7f50052b29f53def02b5cf2160ae49a3d9/codex-rs/core/src/context/multi_agent_mode_instructions.rs) 单独发送协作模式 developer 片段；[`model_messages/multi_agent.rs`](https://github.com/openai/codex/blob/c9750d7f50052b29f53def02b5cf2160ae49a3d9/codex-rs/prompts/src/model_messages/multi_agent.rs) 选择明确请求模式或主动模式；[`session/multi_agents.rs`](https://github.com/openai/codex/blob/c9750d7f50052b29f53def02b5cf2160ae49a3d9/codex-rs/core/src/session/multi_agents.rs) 还接收配置与模型目录覆盖。已检查的这些路径没有强制多一次“直接处理/委派”的模型回合。

Xuloom 增加简化的 `spawn_agent` 模型入口：只需 `prompt`，可选任务标签、角色、参考父历史和重复试验数量。它始终后台启动，返回原生真实 worker ID；等待、消息和取消继续通过 `agent_tool` 完成，旧 API 仍可用。两个入口复用相同的权限、Solo 禁用、排队、输出预算、历史存储和最终等待机制，没有新调度器或递归派发。Ultra 继续独立于模型 effort，不强制数量或为简单对话创建 worker。

实际复测已显示：原模型会忽略 Ultra 软策略，且没有提交被框架拦截的派发请求。简化工具入口降低调用复杂度，不能保证任意模型主动派发；本地确定性模型及协议测试只能验证框架链路。未授权远端模型验收时，不宣称自主调用率已经改善。

## 协议来源

- [OpenAI reasoning](https://developers.openai.com/api/docs/guides/reasoning)：可选 effort 与模型有关，不能推断所有档位都支持。
- [DeepSeek Thinking](https://api-docs.deepseek.com/guides/thinking_mode/)：开关与强度分别控制，OpenAI SDK 的 Thinking 字段经 `extra_body` 发送。
- [Qwen Thinking](https://docs.qwencloud.com/developer-guides/text-generation/thinking)：区分可切换与始终思考模型；本地服务格式需按部署框架确认。
- [Anthropic effort](https://platform.claude.com/docs/en/build-with-claude/effort)：使用 `output_config.effort`；Thinking 与 effort 含义不同。

本次不新增 Responses API 后端，因此依赖 Responses 专有能力的模型仍不属于已完成适配范围。

## 本地验证

Python 3.12 全量 359 项，358 通过、1 跳过；桌面单元测试 50 项通过。协作、推理与供应商契约三组 31 项在暂存资源的包内 Python 中通过。源码 Electron 协作/模型设置端到端通过，验证开关、强度、SDK 发出的实际请求、切换模型、保存配置、窄窗口和停止；一次测试曾在保存完成前读取配置，已改为等待对话框关闭及请求到达后断言。

`spawn_agent` 还会检查原 `agent_tool` 的权限规则，拒绝和询问优先，避免简化入口绕过既有设置。源码记录未包含用户密钥、聊天正文或私人 Skill。

原应用正常退出后，已将原 EXE 资源更新为 beta.22 / 构建 `20261030-173707`，保留原可执行文件和回滚资源。更新后的原 EXE 协作/模型设置端到端、文件端到端及安装资源中的包内 Python 31 项全部通过。没有强制结束用户应用，没有调用远端模型，没有发布新的 Release。

### beta.23：二态开关与控件布局

修复 `.send-options` 未匹配工具栏选择器而继承表单 `width:100%` 的问题：模型名称保留最小显示宽度，Thinking 和 effort 使用紧凑宽度，窄窗口换行。设置页将强度标题与复选项分行布局，并覆盖表单通用的标签和输入间距，去掉空白带。

Thinking 菜单只含开启、关闭；不支持关闭的模型禁用关闭选项。旧 `default` Thinking 配置兼容为开启，避免界面显示开启而请求仍省略开关。effort 的接口默认值保留。源码和原 EXE 的端到端均检查了两项选项、模型名宽度、effort 宽度、复选项垂直间距及实际请求；三种截图已检查。Python 全量 359 项（358 通过、1 跳过），桌面单元 50 项通过，暂存与安装资源中的推理专项各 8 项通过。

已更新同一个原 EXE 至 beta.23 / 构建 `20261030-175257`。未强制结束用户应用，未另建 EXE，未调用远端模型，未发布新 Release。

### beta.24：按选中内容收紧宽度

工具栏选择器使用 `field-sizing:content`，移除模型、协作、Thinking、effort 的固定宽度。短名称按当前内容收紧，模型列表中的长选项不会撑大短名称的关闭状态；长名称仍受响应式上限约束，完整值保留在悬停提示。没有修改模型参数或调用逻辑。

源码与更新后的原 EXE 协作/模型端到端验证了短名称宽度、长名称上限、切换后的尺寸与工具栏无横向溢出，截图已检查。原 EXE 资源为 beta.24 / 构建 `20261030-180325`，保持同一个启动路径，未强制退出用户应用。
