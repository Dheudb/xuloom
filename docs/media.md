# 媒体生成

Xuloom 通过用户配置的服务生成图片、编辑图片、生成视频、语音、音效和音乐。当前协议为 **fal Queue API**，也可连接实现同一协议的自建服务。不是任意 OpenAI 兼容聊天地址都能使用。默认聊天不注入全部媒体工具说明，不下载模型权重、不安装 fal SDK。

## 桌面配置与使用

1. 打开 **设置 → 媒体生成 → 添加生成服务**，填写生成服务的 API 地址和自己的 Key。名称可选，留空显示服务地址中的主机名；连接 ID 自动生成和保存，不需要手填。
2. 添加用途对应的模型，填写服务商提供的实际 `owner/model/endpoint`。不预填任何供应商、API 地址、模型端点或专用参数。同一服务的多个模型共用一个 Key；本地调用名自动提供，可在高级设置调整。
3. 默认参数 JSON 填模型特有字段，如声音、时长、尺寸或歌词。提示词字段通常是 `prompt`，语音通常是 `text`；参考图字段默认是 `image_urls`，部分视频接口为 `image_url` 或 `first_frame_url`。以所选模型的当前官方 schema 为准。
4. 保存后直接发送“生成一张……图片”“把这段文字生成为……语音”等任务。主代理通过工具发现调用 `media_tool`，先检查已配置能力，再使用实际条目提交。

保存、查看配置不会调用服务。编辑 Key 留空保留原值；已保存的 Key 不回传界面或聊天模型。未配置媒体服务不影响普通聊天。编辑已有服务沿用原标识与配置，避免历史任务失去关联；移除默认值不会擅自清除用户已保存的模型。API 地址为空时保存失败，不回退到任何供应商。

API 地址和 Key 确定请求位置及认证；具体模型、输入字段、排队／结果结构仍取决于接口协议。目前仅支持 fal Queue 兼容服务，其他原生生成协议需要各自的适配器，不能只替换地址便宣称兼容。

支持的结果结构为 `images` 数组或 `image` 对象，以及 `video`／`audio` 对象，各产物具有 `url` 字段。不任意遍历 JSON 并下载其中所有地址；其他结构需要增加适配，不能由“同为 fal 模型”推断一定兼容。

生成沿用会话权限、显式拒绝和 Plan 约束，完全访问不覆盖显式规则。审批显示实际服务、模型、参考图片上传路径和输出位置。审批等待期间连接或 Key 改变时拒绝提交，须重新确认。系统执行隔离暂不支持媒体网络生成，会明确拒绝，不退回宿主。

## CLI 配置

CLI 与 App 共享 `~/.xuloom/config.json` 的 `media_connections`，不需要为 CLI 再填一次。建议通过环境变量传 Key，不放进命令参数或公开文件。

将以下示例保存为自己的 `media-connection.json`（它不含 Key）：

```json
{
  "name": "我的媒体服务",
  "protocol": "fal-queue",
  "base_url": "https://your-generation-service.example",
  "network_mode": "direct",
  "models": [
    {"id": "picture", "kind": "image", "model": "your-owner/your-image-model", "defaults": {}},
    {"id": "voice", "kind": "speech", "model": "your-owner/your-speech-model", "defaults": {}}
  ],
  "download_origins": []
}
```

在自己的终端设置 `XULOOM_MEDIA_KEY` 环境变量，然后运行：

```text
xuloom media save --file media-connection.json --api-key-env XULOOM_MEDIA_KEY --json
xuloom media connections --json
xuloom exec "使用已配置的 picture 模型生成一张简洁的山景图片" --cwd /path/to/work --permission full --json
```

请把 `.example` 地址与 `your-owner/...` 替换为自己的真实服务和端点。未提供 `id` 时，CLI 与 App 的保存接口都会自动生成，并在返回值中给出；脚本后续编辑应复用这个标识，也可为已有自动化继续显式指定 `id`。桌面表单的标识在草稿期间保持固定，重复保存不会改成新身份。`connections` 只返回是否已有 Key。模型参数以当前供应商文档为准；示例不提供额度或供应商验收证明。可不使用 `--permission full`，此时按当前审批方式执行。

## 查询与取消，不经过聊天模型

任务的 Xuloom ID 形如 `media-` 加 32 位十六进制字符，供应商 request ID 另行保存。不要互相替换。

```text
xuloom media jobs --cwd /path/to/work --session SESSION_ID --json
xuloom media status MEDIA_JOB_ID --cwd /path/to/work --session SESSION_ID --permission full --wait-ms 0 --json
xuloom media cancel MEDIA_JOB_ID --cwd /path/to/work --session SESSION_ID --permission full --json
xuloom media remove CONNECTION_ID --yes --json
```

`status` 和 `cancel` 恢复原会话的权限和独占写者，通过现有工具执行入口操作，**不请求聊天模型**。默认 `--permission ask`，非交互入口需要审批时返回 `approval_required` 和退出码 3；`--permission full` 仍遵守显式拒绝。CLI 返回的 `reason: completed` 表示这次控制操作结束，生成是否完成看 `generation_completed` 和 `job.phase`。

App 在 **设置 → 媒体生成 → 此会话已保存的生成任务** 展示相同回执，可查询或请求取消。控制时关闭设置以使用正常审批界面，不新增用户 prompt、不清空未发送草稿。刷新列表只读本地记录；没有常驻轮询服务。

删除连接不会取消云端任务，且会使后续查询缺少配置；须恢复原连接/模型后继续查询。旋转同连接 Key 可以继续操作，但供应商账户变更可能导致无权访问原任务。

## 完成、停止与重启

- 提交意图在第一次 POST **之前**可靠保存。拿到真实 request ID 和操作 URL 后再次保存；状态查询不会创建新任务。
- 只有供应商完成且文件下载、校验、发布、记录均成功时才返回 `phase: downloaded`、`finished: true`，产物进入已有“输出与来源”和文件变更入口。供应商 `COMPLETED` 中的错误不算成功。
- 提交超时、断线、响应不完整或提交后保存失败都可能意味着云端已经接受。相同输入的未知结果不会自动再次 POST；明确请求 `retry_unknown` 仍须真实用户审批，即使当前为完全访问。
- 每次最多等待 120 秒，超时返回原任务身份；不等于取消云端。再次查询可继续下载，下载失败也不重新生成。
- 取消回执区分供应商已从队列移除与仅收到取消信号。后者显示 `cancel_requested`，不声称远端已停止。停止聊天只停止本机等待；云端工作可能继续。
- 记录放在中央历史的 `tool-results/<会话>/media-jobs/`，不在项目 `.xuloom` 内。主代理可查看其子代理任务；子代理只能控制自己的任务。聊天分支不复制活跃云任务所有权；移动聊天保留任务及原来已批准的输出位置，不搬运文件。
- 供应商可能清理过期结果。保留身份并报告结果不可用；不会靠重新生成掩盖过期。损坏的任务记录会明确显示，且阻止自动重复提交，不能把记录读失败当作从未提交。

## 文件与播放边界

默认输出 `generated/media-<任务随机ID>-<序号>.<扩展名>`。文件先完整写入同目录临时文件、校验并刷盘，再以不覆盖已有文件的方式发布；已有不同内容不会被替换。重试下载检查大小和 SHA-256。最多八个输出，图片单个 20 MiB、最多四千万像素，音视频单个 256 MiB；本地上传参考图最多四张、单张 5 MiB。

支持 PNG/JPEG/WebP/GIF/BMP、WAV/FLAC/OGG/MP3/M4A/WebA、MP4/WebM。检查实际类型、MIME 和容器；这不是完整编码质量检查。播放依赖 Chromium 支持的编码，不支持或损坏时显示错误，不把原始二进制展示为源码。图片在文件预览中展示；音视频通过可撤销的分块 URL 播放，可 seek、不自动播放，关闭预览即撤销。SSH 使用远端文件字节，不把远端路径当成本机路径。

凭据只送往已配置 API 来源和路径内的队列地址，不跟随带凭据的重定向。产物下载不携带 Key，逐次检查重定向来源；默认允许当前 API 来源、HTTPS `fal.media` 及其子域、`storage.googleapis.com`，其他 CDN 须由用户在连接的下载来源中明确添加。直接连接忽略环境代理；“使用系统代理”显式启用代理环境。

本地 HTTP/SSE 和加密 SSH 测试覆盖请求与故障协议，隐藏 Electron 验证真实 PNG/WAV/WebM 解码和播放。尚未调用真实供应商、验证其收费、可用性或输出质量；MP4 等编码和 macOS/Linux 桌面播放需要各自原生验收。OpenAI 原生图片/语音、其他非 fal 协议没有假装兼容，后续可增量添加适配器。

协议核对与原始参考见 [媒体接口依据](media-adapters-plan.md)。
