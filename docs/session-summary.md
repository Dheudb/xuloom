# 输出与来源

桌面聊天右上角的“输出与来源”展示本轮或整个会话的输出文件、联网来源和工具调用统计。点击文件沿用现有正文/差异预览，点击来源通过系统浏览器打开；工具行打开活动面板并按工具筛选。空白会话不生成占位来源。

## CLI 与 SSH

```bash
xuloom summary --cwd /your/workspace --session SESSION_ID --json
xuloom summary --cwd /your/workspace --session SESSION_ID --all --json
xuloom summary --cwd /your/workspace --session SESSION_ID --turn HUMAN_MESSAGE_UUID --json
xuloom remote summary SERVER_ID --session SESSION_ID --all --json
```

交互 CLI 使用 `/summary` 或 `/summary all`，本地和 SSH 会话共用接口。程序入口是现有 JSONL RPC 的 `session.summary`，参数 `turnId` 和 `all` 互斥；桌面桥按当前会话选择本地或 SSH。查询读取已保存记录，不调用模型，不执行任务，不需要新增模型 Key。远端版本缺少该接口时明确报错，不切换到本地。

## 什么算来源和输出

- 文件来自成功写入工具的结构化回执。摘要保留当时的绝对路径；把聊天拖到另一个项目不会搬动输出文件。文件后来被移动或删除，预览会报告无法读取，不凭历史回执假装文件仍存在。
- 搜索来源是实际返回的搜索结果；“已读取网页”表示抓取工具成功返回页面，不代表论文已完整阅读或结论经验证。相同 URL 优先展示已读取状态。
- MCP 只收集明确的 HTTP(S) `resource_link`。工具正文、网页中的链接和助手回答里的“Sources”不被猜测成来源。禁止脚本、本地文件、带账号密码和控制字符的 URL。
- 工具次数表示保存的回执数，包含失败回执；不是费用、完整审计日志或未完成调用数量。活动详情仍沿用现有近期记录范围。

来源不代替科学验收或任务完成协议。摘要不会把生成文件、找到链接或调用工具等同于完成用户目标。

## 扩展工具如何提供来源

复用已有工具返回协议，无需增加管理器或修改系统提示词：

```python
from xuloom.context.tool_results import build_tool_result

return build_tool_result(
    ok=True,
    content="Actual evidence returned by your service",
    metadata={"sources": [{
        "url": "https://example.org/paper",
        "title": "Paper title returned by the service",
        "kind": "resource",  # search / page / resource
    }]},
)
```

若同一链接已经包含在正文中，可显式增加 `sources_in_content: True`，避免 UI 元数据重复进入模型上下文。默认保留元数据中的来源供模型使用，不能删除扩展工具唯一的证据。只有 `ok=True` 且无错误的回执可产生来源；未知元数据字段不会暴露到摘要。

## 持久化边界

精简索引保存在现有 `metadata.artifacts.references`，按真实用户回合和实际代理身份分组；不另建数据库或执行循环。子代理在入队时绑定父回合，父代理进入下一轮不会改变已有任务的归属。中断任务的已保存子代理回执可被读取，必须验证所属会话。

上下文压缩后仍保留索引；分支和编辑提示词排除未保留的后续回合。保存失败沿用现有失败/不确定语义，摘要只展示磁盘已确认的记录。旧版本没有结构化来源的会话可以显示原有文件/工具记录，但会提示缺失来源，不从文字补造 URL。

本地协议流、真实文件写入、子代理、CLI、加密 SSH 回环与隐藏 Electron 界面均纳入检查；这不代表真实搜索供应商、远端模型或跨平台原生环境已验收。
