# 模型配置与兼容范围

## 连接级请求流控

主代理、子代理、压缩及外部 CLI 中继共用模型 HTTP 入口。同一 API 基础地址与 Key 的连接，在同一个 Xuloom 数据目录下跨会话、跨进程共享请求许可；默认最多 4 个实际请求并行。流式响应在关闭前持续占用许可，进程崩溃由 OS 释放租约。排队按会话轮转，同一会话内先入先出；等待或取消不会新增模型请求。

这不限制 Auto／Ultra 创建的子代理数量，也不改变模型思考强度。429 的 `Retry-After`（秒、HTTP 日期或 `retry-after-ms`）会冷却整个连接；流控自身不会重试生成请求。SDK 原有重试规则保持不变。

可在全局配置中设置 `model_request_limits`，例如 `{"concurrency": 4, "requests_per_minute": 60}`；省略每分钟限制表示不主动限制速率。不同 URL/Key 互不占用对方许可。限流记录只保存哈希身份与排队信息，不保存 API Key。使用不同数据目录或另一台 SSH 主机的进程不共享这份本机队列；供应商账户的总额度仍以服务端为准。

已有客户端会在排队及真正放行前重新读取全局限制，修改限制无需重建客户端。已接纳的在途请求不会被强制中断；新旧客户端同时参与时按其中较严格的限制放行，等较严格的在途请求结束后释放其约束。降低每分钟速率也会按上一次实际启动时间重新计算等待。损坏的队列记录会明确报错并保留原文件，不会悄悄清空后另开一组容量。

## 配置模型

桌面在“设置与模型”配置，CLI 通过 `/model` 配置。两者共用
`~/.xuloom/config.json`。`XULOOM_CONFIG_DIR` 可以为开发或测试指定独立目录。
Windows 保存时使用当前用户的 DPAPI 保护 Key；旧明文配置只读时不改写，下次保存会迁移。macOS/Linux 已有可选系统 Keychain/Secret Service 接入，实际原生平台验收仍需另证；服务不可用时由配置决定拒绝保存或使用文件存储。凭据保护不等于聊天、工具输出与共享产物全部加密。界面只收到 `hasKey` 与不可用状态，不收到 Key 或密文；迁移、备份和修复见[凭据保护](credentials.md)。
已保存的 Key 在输入框中显示遮罩和“已保存”提示，留空保存会保留原值。

模型 ID 用于 API 请求路由，不再自动注入对话的环境提示词。身份回答只介绍 Xuloom，
不主动披露模型 ID、供应商、网关地址或路由，也不读取配置来回答底模身份。
设置、模型选择器和诊断信息仍提供模型配置供本地使用者管理；这项对话约定不是配置访问控制。
系统提示词不指定某一家模型的底层身份，也不根据模型名推测知识截止日期。

## 一个 API 使用多个模型

“设置与模型 → 模型连接”中可为每个 API 填写连接名称、协议、地址和 Key，并在“可选模型列表”中每行填写一个模型 ID。
当前“模型 ID”会自动加入列表。点击“从 API 获取模型”可读取该服务的 `/models` 列表，合并到输入框；点击“保存配置”后才生效。
模型是否可用、是否支持工具调用，以及免费额度由提供方决定；列出模型本身不会发起聊天请求。

聊天输入框旁的模型选择器按 API 分组，切换后沿用当前会话、历史和草稿。运行中的任务和子代理需完成或停止后再切换。
“新增 API”可保存另一组独立的地址、Key 和模型列表，切换 API 时凭据随连接切换。
选中已保存的连接后，点击“删除 API”并确认，可删除该连接、Key 和全部模型列表。
删除当前连接时自动切换到剩余连接；删除其他连接保留当前选择。所有任务需停止后才能删除。
删完全部连接会回到未配置状态，重启不会从旧字段或进程环境恢复已删除的 API；可以重新添加连接。
删除 API 不删除聊天记录和工作文件。
CLI 使用 `/model list` 查看同一份模型列表，`/model use 2` 选择编号；原有 `/model` 配置向导仍然可用。

配置结构如下（示例 Key 需替换，实际密钥仅保存在本机）：

```json
{
  "active_model_connection": "my-api",
  "model_connections": [{
    "id": "my-api",
    "name": "我的 API",
    "provider": "openai",
    "base_url": "http://localhost:8000/v1",
    "api_key": "local-placeholder",
    "model": "model-a",
    "models": ["model-a", "model-b", "model-c"],
    "network_mode": "direct"
  }]
}
```

界面保存/切换时还会同步 `model_provider`、`openai_model`/`anthropic_model` 等原有字段，兼容 CLI 启动和旧配置。
旧单模型配置首次保存时自动迁移，无需重新填写 Key。

## 支持的接口

| 接口 | 当前实现 |
|---|---|
| Anthropic Messages | 流式对话、工具调用及相关消息格式 |
| OpenAI 兼容 Chat Completions | 文本流、工具定义与工具调用结果 |
| 仅提供 Responses 的接口 | 尚未实现 |
| 其他厂商原生接口 | 需要适配，或使用其兼容端点 |

模型连接可以在打开项目前保存。选择 OpenAI 协议时，推荐填写 API 根地址，例如 `http://localhost:8000/v1`；
也接受完整的 `/chat/completions` 地址，保存时会自动转换为根地址。填写服务实际提供的模型 ID。
如果本地服务不校验 Key，可填写占位值；SDK 初始化仍可能要求非空 Key。

## 环境变量

```powershell
$env:XULOOM_MODEL_PROVIDER = "openai"
$env:OPENAI_BASE_URL = "http://localhost:8000/v1"
$env:OPENAI_MODEL = "your-served-model-id"
$env:OPENAI_API_KEY = "local-placeholder"
```

Anthropic 对应 `ANTHROPIC_MODEL`、`ANTHROPIC_API_KEY`、`ANTHROPIC_BASE_URL`。
CLI 启动时，已设置的环境变量优先于从配置文件注入的值。桌面会话使用设置页保存的连接配置，
恢复历史时也使用当前连接配置，避免旧历史里的地址覆盖新设置。
明确保存的空连接列表表示用户已删除全部 API，此时会清理旧模型环境变量，需重新配置后才能调用模型。

## OpenAI 兼容流的结束与等待

收到 `finish_reason` 后，最多额外等 1 秒接收统计信息；网关不关闭连接也会结束本次接收。
未收到结束标记便断开，或持续无有效数据，则显示中断并保存部分回答，不自动重放请求或工具操作。
默认最多等 120 秒接收首个有效数据；已有数据后连续 60 秒没有新文本、推理数据或工具参数就中断。
空包、SSE 心跳不能延长这个时间；持续收到推理数据不会被当成卡住。此规则目前用于 OpenAI 兼容接口。
可在本机配置文件覆盖 `stream_first_response_timeout_sec`、`stream_idle_timeout_sec`、`stream_end_grace_sec`，
或设置对应的 `XULOOM_STREAM_FIRST_RESPONSE_TIMEOUT_SEC`、`XULOOM_STREAM_IDLE_TIMEOUT_SEC`、`XULOOM_STREAM_END_GRACE_SEC`；
数值需大于 0、最多 3600 秒。工具执行和子代理等待采用各自的机制。
运行时显示等待接口、推理和接收阶段，执行时长旁的提示可查看最近请求的首字耗时；
历史保存接收时间与数量，不把推理原文作为回答或活动日志展示。
对于会返回 `reasoning_content` 的 OpenAI 兼容接口，完整字段保存在本地会话的内部协议记录中，
在后续工具请求中按原地址、Key 的哈希身份与模型回传；不会带到其他连接或模型。
备份、共享完整会话或录像时，这些文件可能包含供应商要求保留的原始协议内容。

DeepSeek 的 Chat 思考模式不接受 `tool_choice=required`；Ultra 使用 `auto`，由现有循环检查协作评估和等待结果。
接口支持兼容协议不代表支持所有可选参数，不通过关闭思考绕过限制。
旧程序已经丢失的协议字段不能还原；若旧会话仍提示缺少 `reasoning_content`，保留原记录并从新会话继续。

## 直连与代理

模型请求默认直连，不会自动读取 Windows 系统代理。设置页可改为“系统代理 / 环境代理”，
此时会使用系统代理以及 `HTTP_PROXY`、`HTTPS_PROXY`、`NO_PROXY`。
CLI 可用 `XULOOM_NETWORK_MODE=system` 启用代理，或 `direct` 直连。
主代理、子代理和上下文压缩共用此规则；证书校验始终开启。

## 兼容边界

设置中可按连接和模型声明 Thinking 开关、推理强度和参数格式，保存后从输入框选择。
桌面与 CLI 共用配置；具体用法与限制见 [模型思考控制](history/model-reasoning-2026-10-30.md)。

能返回聊天文本，不等于能完整执行编程任务。需要分别验证文本流、工具调用、工具结果回传、
Skill 指令和子代理执行。设置中可按模型声明仅文本或图片能力；已知不支持的输入会在请求前拦截，未知能力仍由接口判断，不能根据名字猜测。文件工具和 MCP 图片保留媒体块，OpenAI Chat 路径可转为 image_url。
OpenAI Chat 尚未接入原生文档输入；PDF 仍可本地提取文本。上下文大小、输出上限和推理字段也不保证任意供应商完全兼容。详见 [输入与恢复说明](history/input-and-recovery-hardening-2026-10-30.md)。

自定义子代理可以选择模型 ID，连接协议和服务地址沿用父引擎，不能据此任意跨供应商切换。
本地模型的联网搜索由 Xuloom 的独立工具或 MCP 完成，仍要求推理接口正确输出工具调用。


## 上下文窗口自动识别

普通会话的累计输出 Token 仅用于用量统计，不设置默认的会话终身输出额度；达到历史累计 200,000 tokens 不会阻止新任务或续写，恢复旧检查点也无需清零统计。每次模型请求仍遵守单次输出限制；用户显式设置的目标、委派及费用预算仍会限制执行。这些限制与上下文窗口分别计算。

窗口是每个 API 连接下每个模型的设置，不是 API Key 的全局属性。优先使用已填写的窗口；没有显式值时，官方连接使用已核对的官方规格，已知开源模型使用发布者的原生窗口，其他模型保守回退 65,536 tokens。不会自动把可通过 RoPE/YaRN 扩展的上限当作已启用容量，也不从模型名称中的参数规模猜窗口。

设置中的“从 API 获取模型”复用 GET /models，同时识别 context_window、context_length、max_model_len 等明确字段（也支持 limits、top_provider、architecture、text_config 下的这些字段）。多个有效声明冲突时取较小值；最大输出和仅输入上限不冒充总窗口。只补充空窗口，已有手动值保留，点击保存后才写入配置。不后台请求模型、不消耗生成 Token，不改变思考或 Chat/Responses 协议。

2026-11-12 核对的发布规格包括：[OpenAI GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol) 等已配置 GPT-6 系列（1,050,000），[DeepSeek](https://api-docs.deepseek.com/quick_start/pricing/)（接口明确返回 1,048,576），[Qwen3.6-35B-A3B](https://huggingface.co/Qwen/Qwen3.6-35B-A3B)（262,144 原生），[Agents-A1](https://huggingface.co/InternScience/Agents-A1/blob/main/config.json)（262,144），[Intern-S2-Preview-397B](https://huggingface.co/internlm/Intern-S2-Preview-397B/blob/main/config.json)（262,144），[Intern-S1](https://huggingface.co/internlm/Intern-S1/blob/main/config.json)（65,536），[GLM-5.2](https://huggingface.co/zai-org/GLM-5.2) / [GLM-5.3](https://huggingface.co/zai-org/GLM-5.3)（1,048,576），[Kimi-K2.6](https://huggingface.co/moonshotai/Kimi-K2.6)（262,144），[MiniMax-M3](https://huggingface.co/MiniMaxAI/MiniMax-M3)（1,048,576）。发布规格是未声明服务端限制时的名义默认，服务端可能部署更短窗口；可随时按实际部署调整。私人微调模型只在用户自己的配置中填写基模对应窗口，不把私人别名加入公共代码。
