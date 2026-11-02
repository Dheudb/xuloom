# 查看子代理完整历史

桌面在“代理 → 会话任务”中提供 **查看完整对话历史**。本机原生子代理及可选的 Claude Code、Pi、Codex、DSH 后端使用同一个只读入口；SSH 子代理从其原来绑定的执行主机读取。查看不会发送 prompt、恢复代理、重放工具、申请写者锁或更新运行中引擎的保存版本。

历史窗口展示已保存的任务输入、回答、模型公开的推理摘要、工具参数与返回结果。工具和推理默认折叠，内容可复制；图片点击后才读取。系统指令、连接配置和传输元数据不作为对话显示。未公开的推理无法还原，尚未支持的内容块会明确提示，不会假装已有预览。

## CLI

```sh
# 当前目录中已保存的主会话
xuloom transcript --session SESSION_ID --json

# 已登记在此主会话下的一个子代理
xuloom transcript --cwd /path/to/work --session SESSION_ID --agent AGENT_ID --limit 40 --json

# 继续读取返回值中的 nextCursor；始终使用同一父会话及 agent
xuloom transcript --session SESSION_ID --agent AGENT_ID --cursor PAGE_CURSOR --json

# 查看另一台机器上已保存的会话／其子代理
xuloom remote transcript lab --session SESSION_ID --agent AGENT_ID --json
```

在本机或 SSH 交互式终端中也可以输入 `/history`、`/history AGENT_ID`，或 `/history AGENT_ID PAGE_CURSOR`。`/history - PAGE_CURSOR` 继续主会话的历史分页。省略 cursor 总是重新读取当前快照。

## 读取约束

- 页大小为 1–100 段，默认 40。单个长文本分段传输，一页文本至多约 64K 字符；后续内容由 `nextCursor` 继续获取。这个限制是界面传输分页，不是模型输出预算，也不会修改或截断原始文件。
- 页游标绑定父会话、子代理、后端及文件内容。历史被续写、替换或删除后，旧游标明确报错；刷新可读取新快照，不会自动重试执行任务。正在写入的最后一条 JSONL 不会当成完整记录；若记录显示已结束但尾行损坏，会报告错误。
- 读取原生 CLI 的会话日志，不把流式传输里的 delta 和最终消息重复展示。按日志保存顺序展示，不能据此推断当前执行已经完成或可安全恢复。
- 身份必须是已保存主会话下登记的子代理；不接受任意日志路径。不跟随历史目录内的符号链接或 junction。外部日志必须位于对应会话的集中产物目录，原生日志头也必须匹配其后端身份。
- 图片使用已保存内嵌内容，或 DSH 已保存的、经字节数及 SHA256 核对的不可变附件。不自动下载外部图片 URL。图片仍使用现有大小、像素和预览约束。
- 单份日志显示上限 256 MiB、单条 JSONL 上限 32 MiB。超过限制会明确报告，并保留原始文件供开发者处理，不返回假装完整的截断结果。

这是已保存记录的查看能力，不是外部 CLI 未知中断恢复机制。物理跨机、POSIX 原生界面及真实供应商长任务仍按[完整目标](product-roadmap.md)分别验收。

## 程序接口

桌面与 SSH 复用 `session.transcript`，参数为 `agentId?`、`cursor?`、`limit?`；会话由已打开的工作台确定，不接受前端任意切换存储路径。返回 `entries`、`version`、`nextCursor`、`readOnly`、`live`、`partial` 和说明。`session.transcriptImage` 使用同一 `agentId`、该页的 `version` 和图片的 `imageIndex`；过期快照不能套用到另一张图片。

这些接口直接读取原有 JSON／JSONL，没有第二套会话数据库、执行队列或恢复状态机，也不安装任何外部 CLI。
