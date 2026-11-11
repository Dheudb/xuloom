# HTTP 服务模式

`xuloom serve` 用 HTTP 提供会话、任务和审批接口，事件通过 Server-Sent Events（SSE）流式返回。服务只使用标准库实现，不增加依赖，适合把 Xuloom 接入 Web 后端、其他语言的程序或内部工具。

```bash
export XULOOM_SERVER_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
xuloom serve --root ~/work --port 8765            # 默认只监听 127.0.0.1
```

模型连接与 CLI 相同（`~/.xuloom/config.json` 或环境变量）。不设置 `XULOOM_SERVER_TOKEN` 时，服务启动时会生成一个随机 token 并打印到 stderr。

## 安全边界

- 除 `GET /v1/health` 外，所有请求都需要 `Authorization: Bearer <token>`；token 至少 16 个字符，比较时使用常量时间。
- 每个会话的工作目录必须位于 `--root` 之内。
- 默认 `--permission ask`：需要审批的操作会作为 `approval_required` 事件发给客户端，客户端答复后才会执行。超过 `--approval-timeout`（默认 600 秒）没有答复的请求按拒绝处理。
- 不返回 CORS 头，浏览器页面无法跨站调用。
- 服务没有 TLS。监听非 loopback 地址（`--host 0.0.0.0`）等于把工具执行能力开放到网络，服务启动时会给出警告。需要远程访问时，请放在反向代理（TLS）或 SSH 隧道之后。
- 请求体最大 10 MB，不支持 chunked 请求体。每个连接只处理一个请求。

## 接口

| 方法与路径 | 请求体 | 返回 |
| --- | --- | --- |
| `GET /v1/health` | | `{"ok": true}`（无需 token） |
| `POST /v1/sessions` | `cwd`（相对 root，可选）、`session_id`（重新打开已保存的会话）、`permission`（`ask`/`full`）、`collaboration_mode` | 201，会话描述 |
| `GET /v1/sessions` | | `{"sessions": [...]}` |
| `GET /v1/sessions/{id}` | | `session_id`、`cwd`、`running`、`pending`（待审批与待回答）、`usage`、`cost_usd` |
| `POST /v1/sessions/{id}/messages` | `text`、`stream` | 见下文；已有任务在运行时返回 409 |
| `POST /v1/sessions/{id}/approvals/{request_id}` | `decision`：`allow`、`always`（本会话内记住）、`deny`、`never` | `{"resolved": ...}` |
| `POST /v1/sessions/{id}/answers/{request_id}` | `answer` | 回答模型通过 `ask_user` 提出的问题 |
| `POST /v1/sessions/{id}/abort` | | 202，请求停止当前任务 |
| `DELETE /v1/sessions/{id}` | | 204，停止任务、拒绝未答复的审批并关闭会话 |

发送任务时：

- **流式**：`"stream": true` 或 `Accept: text/event-stream`。每个事件是一条 `event: <type>` 加 `data: <JSON>` 的 SSE 消息，事件类型与 `engine.submit_message` 相同，另有 `approval_required`、`input_required` 和 `request_expired`。流以 `final` 事件结束。客户端在 `final` 之前断开连接会停止该任务，与关闭 `submit_message` 消费者的行为一致；服务在等待下一个事件时就会察觉连接关闭（例如模型仍在生成时），不必等到下一次写入失败。阻塞模式的客户端断开时同样停止任务。发送 SSE 响应头时连接就已重置的情况也一样：任务即使还没开始执行也会被取消，取消结果照常保存。
- **阻塞**：其他情况下，服务等任务结束后返回 `{"text", "result"}`。这期间如果出现审批请求，需要另一个连接通过 `GET /v1/sessions/{id}` 的 `pending` 发现并答复。

```bash
H="Authorization: Bearer $XULOOM_SERVER_TOKEN"
SID=$(curl -s -H "$H" -X POST localhost:8765/v1/sessions -d '{"cwd": "repo"}' | jq -r .session_id)
curl -N -H "$H" -X POST localhost:8765/v1/sessions/$SID/messages -d '{"text": "运行测试并修复失败项", "stream": true}'
# 收到 approval_required 后：
curl -H "$H" -X POST localhost:8765/v1/sessions/$SID/approvals/<request_id> -d '{"decision": "allow"}'
```

在 Python 中可以直接嵌入：`XuloomServer(config, root=..., token=...)`，然后调用 `await server.start()` / `serve_forever()` / `close()`。

当前不提供 WebSocket；SSE 加 POST 已经覆盖双向交互，并且不需要额外依赖。会话只保存在服务进程内存中，服务重启后可以用 `session_id` 重新打开已保存的会话。
