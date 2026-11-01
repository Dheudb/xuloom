# 媒体接口核对与实现约束

核对日期：2026-11-01。本文保留供应商协议核对依据。v0.1.5 已实现 fal Queue API 薄适配器、配置入口、持久化回执及媒体预览，使用方法见 [媒体生成](media.md)。OpenAI 图片／语音独立协议尚未接入；没有调用真实供应商或用户 Key，本地协议验收不代表真实供应商成功。

## 已核对的官方信息

| 能力 | 接口事实 | 对 Xuloom 的影响 |
|---|---|---|
| 图片 | 图片生成有独立 `POST /images/generations`，需要模型与 prompt；返回数据可含 base64 图像 | 作为独立媒体能力配置，不能只因聊天接口兼容就启用。模型 ID 由用户填写，不预设已下线旧模型。[接口参考](https://developers.openai.com/api/reference/resources/images/methods/generate) |
| OpenAI 旧视频接口 | 官方弃用记录列明 Videos API 及 Sora 2 于 2026-09-24 下线 | 不能拿仍能搜到的历史示例作为当前可用默认适配器；视频需继续核对仍可用供应商的真实提交/查询/下载协议。[弃用记录](https://developers.openai.com/api/docs/deprecations) |
| 语音 | 现有 Speech API 与 Realtime 是不同工作流；文档给出的新模型迁移不能直接替换进旧 `/audio/speech` 请求 | 设置需要明确协议，不能只换 model name。旧协议兼容与新的音频事件流程分开适配；模型可用性在实施时再次核对。[语音指南](https://developers.openai.com/api/docs/guides/text-to-speech)、[Speech 参考](https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create) |
| 音乐 | 本次已打开的 OpenAI 文档未建立独立音乐生成接口的可用合同 | 继续寻找并核对音乐供应商接口；不把 TTS、声音样本或纯文字歌词算作音乐生成。不能从“支持音频”推断支持音乐。 |

这些事实不用于限制用户填写其自有兼容服务，但兼容协议与官方服务当前可用性必须区分。不得自动重写用户明确指定的模型或把它暗中路由到其他供应商。

## 薄适配器的边界

- 连接定义分别声明图片、视频、语音或音乐能力及实际协议，沿用现有实例级凭据/网络配置。默认聊天不携带所有媒体说明；按现有工具发现机制暴露已配置能力。
- 主代理仍自主决定是否调用。生成工具接收真实 prompt 和必要参数，返回产物或任务回执，不靠模型编造文件路径、任务 ID 或成功状态。
- 异步接口先保存供应商任务 ID 和提交状态，再查询或延迟唤醒。优先扩展现有执行记录；不增加平行的任务管理器或常驻轮询引擎。
- 提交后断线不能自动再次创建付费任务。供应商支持幂等时使用稳定身份；不支持且无法查询时明确标为结果未知。取消须区分本机停止等待与供应商确认取消。
- 下载使用受约束的文件和网络路径，校验来源、格式、大小与完整性；跨域或重定向不得附带原连接 Key。结果保存失败不能报告完整成功。
- App 提供图片、音频、视频产物预览；CLI 返回同一文件和任务身份。二者共享配置与执行，不能为桌面另建生成循环。文本模型可调用外部生成能力，但不能据此声称能理解返回图片。
- 使用已有权限和隔离入口；工作区隔离模式未支持的网络后端明确不可用，不偷偷切回宿主执行。媒体文件不进入 Skill 安装包或公开仓库。

## 实现验收

每个适配器覆盖未配置、错误字段、鉴权失败、成功产物、取消、超时、错误 MIME、超大响应、下载/保存失败，以及异步提交后进程重启与结果未知。先用本地协议服务检查 App/CLI 一致性；真实供应商调用须另有用户授权，不能用本地替身宣称真实模型生成成功。

## 已实现适配器的协议依据：fal Queue

2026-11-01 已核对 fal 官方队列 [提交](https://fal.ai/docs/api-reference/inference-api/queue/submit)、[状态](https://fal.ai/docs/api-reference/inference-api/queue/status)、[结果](https://fal.ai/docs/api-reference/inference-api/queue/result) 和 [取消](https://fal.ai/docs/api-reference/inference-api/queue/cancel)。实现采用这些返回地址和状态语义；尚未调用真实服务。

- 提交返回真实 request ID 及状态、结果、取消 URL；采用返回地址，不能简单把含子端点的模型名拼成查询地址。凭据只发往已配置的队列来源，下载产物不携带队列 Key。
- `COMPLETED` 同样可能表示失败或取消；必须检查错误和最终结果，确认文件保存后才能报告成功。取消信号已发送不等于远端已停止。
- 当前提交文档没有建立可依赖的幂等键合同。提交前保存意图，提交超时或断线显示未知，不自动重新 POST。结果有保留期限；过期时保留任务身份并报告不可用，不能重做一次生成。
- [FLUX.2 编辑](https://fal.ai/models/fal-ai/flux-2/edit/api) 接受 prompt 和 image_urls（最多四张）；文件输入可用数据 URI。[MiniMax Speech](https://fal.ai/models/fal-ai/minimax/speech-02-hd/api) 接受 text、声音设置并返回 audio；[Music 2.6](https://fal.ai/models/fal-ai/minimax-music/v2.6/api) 使用音乐 prompt、歌词或纯器乐选项，输出独立 audio。两种音频能力不能混同。
- [旧 Veo 3 fast](https://fal.ai/models/fal-ai/veo3/fast/api) 已弃用；[Veo 3.1 fast API](https://fal.ai/models/fal-ai/veo3.1/fast/api) 也明确预告 2026-11-17 弃用。因此不将这两个短期/失效端点设为产品默认值；模型由用户明确选择，实施时再核对当前 schema 和替代端点。

实现复用现有权限、中央产物存档、文件事件和 App/CLI 入口，不安装 fal SDK 或模型权重。适配器与 UI 的本地验证记录另列，不能将本文的文档核对当成真实供应商验收。
