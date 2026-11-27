# 生成模型

## CLI 模型选择与直接生成

交互终端输入 `/media`，按用途选择已有模型、添加／编辑／移除配置、生成，以及查询或取消本会话已有任务。配置与桌面共用，选择菜单不访问模型或生成服务。图片可填写尺寸和比例、附参考图，视频可填写时长和比例，语音可选择音色和速度；参数支持范围由现有供应商适配器校验。

脚本生成不经过聊天模型，使用 `xuloom media models --json` 返回的 `connectionId` 和内部模型 `id`：

```powershell
xuloom media generate --connection CONNECTION_ID --model MODEL_ENTRY_ID --prompt "生成内容" --permission full --json
xuloom media generate --connection CONNECTION_ID --model MODEL_ENTRY_ID --prompt "修改参考图" --image "C:\data\reference.png" --inputs '{"size":"1024x1024"}' --permission full --json
xuloom media remove-model --connection CONNECTION_ID --model MODEL_ENTRY_ID --yes --json
```

非交互默认 `--permission ask`，需要审批时返回 `approval_required`，不会偷偷提交；`full` 也不能绕过 Plan、角色或系统隔离。新任务返回 `session_id` 和持久任务回执；有 `job.id` 时后续使用 `media status/cancel JOB_ID --session SESSION_ID`，不重复生成。`generation_completed` 只有文件已下载并校验成功才为 true。更改配置不修改已有任务冻结的目标；删除配置不会取消供应商任务。未配置对应供应商或账户权限时不会因为出现菜单就获得生成能力。

## 桌面与共享配置

媒体生成由内置插件 `media` 提供（`xuloom/media/plugin.py`），默认开启。不需要时运行 `xuloom plugins disable media`：之后新会话不再提供 `media_tool`，`xuloom media` 命令与桌面端的生成模型设置接口也会停用；已保存的连接与任务记录保留，重新启用即可恢复。

在 **设置 → 生成模型**，分别为图片、视频、语音、音效、音乐添加模型。每个模型只填写 **API 地址、API Key、模型名**。所有输入初始留空，不预填供应商，不需要创建“媒体服务”、填写连接 ID、选择协议、设置输入字段或编写参数 JSON。

保存不调用供应商，也不自动试生成。保存后直接在对话中描述需求，例如“生成一张横版图片”“把这段话读出来”“生成五秒雨声”。尺寸、时长、音色等由主 agent 使用统一工具参数表达，框架按已适配接口转换，校验不支持的参数后才提交。图片类别包含参考图编辑；必须使用支持编辑的模型。

## API 兼容范围

| 接口格式 | 用途与请求处理 |
| --- | --- |
| 标准 Images 接口 | 根地址自动拼接 `/images/generations`；有参考图时使用 `/images/edits`，自动处理 multipart；接受 Base64 或 URL 图片 |
| 标准 Audio Speech 接口 | 自动拼接 `/audio/speech`；文字、音色、语速由适配器转换，接收实际音频字节 |
| 标准 Videos 接口 | 自动使用 `/videos` 的 multipart 提交、任务查询及 `/content` 下载，不将删除视频当作取消生成 |
| MiniMax 原生接口 | 自动识别官方根地址、`/v2/video_generation` 与 `/v1/t2a_v2`；H3 视频按任务 ID 查询，语音 hex 解码后保存为 WAV |
| fal Queue 及同协议服务 | 使用填写的模型名作为 endpoint，读取公开模型 schema 后映射字段、默认值和限制；已有显式配置继续兼容 |
| 已适配的音频接口 | 支持 `/sound-generation` 音效、`/music` 音乐、`/text-to-speech` 语音；自动转换音频鉴权和参数；需要音色时读取该服务可用声音 |

可填写 API 根地址，也可填写上述完整生成接口地址，程序不会重复追加路径。根地址应包含服务商要求的前缀（通常为 `/v1`）；实际模型名必须由服务商提供。

**自动处理不等于所有互联网 API 都天然兼容。** 除已适配的标准及服务接口，未知专有协议仍需框架增加适配。普通聊天地址不会自动变成图像／视频模型。无法识别的音效、音乐接口会在生成前提示；不会依次向多个路径提交付费任务来猜测协议。自建兼容服务可公开 `/openapi.json` 帮助自动识别。公开 schema 读取不携带 Key、不跟随跨域重定向，也不会执行 schema 中的远程引用。

fal 官方队列从公开 schema 自动读取所填写模型的字段。模型要求框架尚未覆盖的必填输入时会明确拒绝，不要求用户在设置中写 JSON，不虚构默认值。音效、音乐和语音是不同用途，可共用服务和 Key；同一地址与 Key 自动复用内部存储，界面始终以模型为入口。修改一个模型的地址、名称或 Key 不会改掉同组其他模型。

## MiniMax 视频和语音

官方中国区填写 `https://api.minimax.cn`（或对应完整生成接口）、Key、模型名即可。H3 视频当前支持 `MiniMax-H3`、`MiniMax-H3-Highspeed`、`MiniMax-H3-Lite`、`MiniMax-H3-Max`，不把旧 Hailuo 接口当成 H3。使用 768P，默认 5 秒、16:9；通过已有工具参数可指定时长和常用宽高比，最多上传四张本地参考图片。Max 最短 5 秒，其余最短 4 秒，最长 15 秒；2K 和视频／音频参考输入暂未接入。

同步语音采用 `/v1/t2a_v2`，一次最多 10000 字符。框架选择官方系统音色 `Chinese (Mandarin)_Lyrical_Voice`，可通过已有 `voice` 覆盖；语速支持 0.5–2。完整响应先落盘，再解码和校验 WAV；保存失败可继续原任务，不再生成。HTTP 200 中的服务错误码也按失败处理，错误正文不回显。

视频查询不重新提交，也不会把供应商完成等同于本地保存成功。MiniMax 的 DELETE 对排队任务表示取消、对完成任务却表示删除；接口没有条件取消，查询与删除之间存在竞态。因此这里不发送 DELETE 来模拟取消，明确提示用户检查供应商；停止本地等待仍不会停止远端生成。

这是视频与语音的适配，不包含音色复刻、音色设计、MiniMax 音乐或音效。订阅 Key 的模型权限由套餐决定；保存配置不代表账户已获全部权限。实现依据：[视频创建](https://platform.minimax.cn/docs/api-reference/video-generation-v2-create)、[任务查询](https://platform.minimax.cn/docs/api-reference/video-generation-v2-query)、[取消语义](https://platform.minimax.cn/docs/api-reference/video-generation-v2-delete)、[同步语音](https://platform.minimax.cn/docs/api-reference/speech-t2a-http)。

## 配置与凭据

Key 只保存在本机私有配置，不返回到前端或聊天模型。Windows 保存时用当前用户的系统原生加密保护字段；其他平台当前仍为本机文件存储。编辑 Key 留空保留原值，连接与模型的内部 ID 自动维护。原有服务／模型配置仍可显示在对应分类，未编辑的历史任务保持绑定。凭据无法解密时仍显示模型和地址，生成前明确拒绝，可编辑并重新填写；详见[凭据保护](credentials.md)。

API 模型切换不会带走聊天模型的凭据；生成请求只使用该生成模型的 Key。接口凭据不附带到产物 CDN 下载。下载仅接受配置 API 同来源、兼容服务已有白名单及受支持的生成 CDN，拒绝未知来源，不因结果里出现 URL 就无条件下载。

## CLI：不需要写配置 JSON

先在自己的终端设置含 Key 的环境变量，然后运行（示例均为占位名称，不是预配置）：

```sh
xuloom media add --kind image --api-url YOUR_API_ADDRESS --model YOUR_MODEL --api-key-env MY_MEDIA_KEY --json
xuloom media add --kind video --api-url YOUR_API_ADDRESS --model YOUR_MODEL --api-key-env MY_MEDIA_KEY --json
xuloom media models --json
```

`--kind` 使用 `image / video / speech / sound / music`。交互终端也可省略 `--api-key-env`，程序隐藏输入 Key；无交互环境必须指定环境变量，不把 Key 放进命令参数。编辑已列出的模型可同时传入 `--connection` 和 `--id`，其他字段相同。旧的连接 JSON 导入命令仅为已有集成保留，不是日常配置要求。

App 和 CLI 使用同一配置。生成仍通过会话及 `media_tool`，支持能力发现、提交、查询和请求取消。任务查看与控制：

```sh
xuloom media jobs --cwd WORKSPACE --session SESSION_ID --json
xuloom media status --cwd WORKSPACE --session SESSION_ID JOB_ID --permission full --json
xuloom media cancel --cwd WORKSPACE --session SESSION_ID JOB_ID --permission full --json
```

控制操作不新增聊天 prompt、不清空草稿、不调用聊天模型。无交互权限需要批准时返回 `approval_required`。删除模型不会取消供应商已有任务，且会使其后续查询缺少原配置，删除前界面要求确认。

MiniMax 官方连接内置允许图片 CDN `hailuo-image-algeng-data.oss-cn-wulanchabu.aliyuncs.com`（HTTPS 443），不放行整个阿里云 OSS 域名。下载被拦截时，响应保留在原任务；修复后查询原任务即可继续下载，不需要重新提交生成。

## 可靠性与完成

- 每次生成先保存提交意图，**只发一次 POST**；没有应用层生成重试或协议回退。超时、断线、响应丢失时保留未知结果，不能冒充失败后偷偷重试。
- 相同输入的未知任务不自动重新提交；显式 `retry_unknown` 仍需实际批准。参考图片字节参与请求摘要。
- 同步接口先把完整响应保存到中央任务产物，再解码或发布。下载／磁盘失败后查询原任务可复用响应，避免再次付费生成；保存结果受大小和 SHA-256 校验约束。
- 异步视频查询与内容下载绑定同一真实任务 ID；对不上就停止。供应商没有取消接口时明确说明，不发 DELETE 来假装停止。
- 只有完整产物校验和保存成功才返回 `downloaded / finished: true`。支持现有“输出与来源”、文件变更和图片／音视频预览，不把供应商已完成等同于本地文件已保存。
- 停止本地等待不等于供应商停止。已有 fal 队列取消语义保持：区分已取消和仅提出请求。没有常驻轮询服务。
- 生成遵守会话权限、Plan 及显式拒绝；审批期间模型地址或 Key 改变会阻止原请求。系统隔离模式尚不支持媒体网络生成，明确拒绝而不降级宿主执行。

中央会话产物保存任务回执，未写入项目 `.xuloom`；私人 Skill、模型 Key 和聊天记录不会打包进 EXE。单图限制 20 MiB、四千万像素，音视频 256 MiB；参考图最多四张、每张 5 MiB。类型与容器检查不能替代完整内容质量评估。播放取决于 Chromium 支持的实际编码。

实现核对：[Images](https://developers.openai.com/api/reference/resources/images)、[Speech](https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create)、[Videos](https://developers.openai.com/api/reference/resources/videos/methods/create)、[fal inference](https://fal.ai/docs/documentation/model-apis/inference)、[音效接口](https://elevenlabs.io/docs/api-reference/text-to-sound-effects/convert)、[音乐接口](https://elevenlabs.io/docs/api-reference/music/compose)。这些是框架适配依据，不是默认配置。
