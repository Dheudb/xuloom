# MCP OAuth 登录

适用于需要浏览器账号授权的 **HTTP / Streamable HTTP MCP 服务**。不会把 MCP 的访问令牌发给聊天模型，也不会把它当作模型 API Key。stdio 服务继续使用原有环境变量配置。

**Xuloom 本身不需要注册账号。**这里的 Client ID 是第三方授权服务登记的应用身份，只有选择该类服务时才需要；它不是 Xuloom 账号。普通模型 API、手工 Key / 请求头 MCP 和本地程序无需使用此入口。

## 桌面

1. 打开 MCP 市场 → 自定义服务，填写服务标识和 HTTPS 地址。
2. 认证方式选择 **OAuth · 浏览器登录**，保存配置。保存不会登录或启动服务。
3. 在“我的服务”点击 **登录**，再点击 **打开授权页面**；在服务网页确认账号与权限。
4. 浏览器成功返回后，列表显示“已授权”。可以测试连接、启用或刷新连接。

登录只进行授权、资源 GET 探测和元数据发现，不调用 MCP 工具、聊天模型或 `initialize`。GET 被禁用的服务直接走标准资源元数据发现，避免仅为登录创建服务端会话。后台连接只使用已有授权、必要时刷新；未登录或需要扩大授权时，明确要求回到设置，不擅自打开浏览器或注册客户端。

**SSH：**登录状态、系统凭据和回调监听都位于执行主机。本地浏览器无法访问远端 `127.0.0.1` 时，展开“SSH / 手动完成回调”，粘贴浏览器地址栏的完整回调 URL。不要粘贴到聊天输入框。回调经已有 SSH RPC 通道提交并校验；授权码不会保存为聊天消息。

取消会停止等待并关闭监听。取消与成功保存同时发生时，以列表账号状态为准；若要清除已保存的授权，使用“退出登录”。提供商可能已创建账号授权，取消本地等待不代表服务端已经撤销。

## CLI

```sh
# 使用你自己的 MCP 地址；新服务默认停用。
xuloom mcp add research --cwd /path/to/project --url https://mcp.example.com/mcp --oauth --json
xuloom mcp login research --cwd /path/to/project
xuloom mcp list --cwd /path/to/project --json
xuloom mcp logout research --cwd /path/to/project --json
xuloom mcp remove research --cwd /path/to/project --json
```

`login --no-browser --json` 输出单独的 `mcp_oauth_authorize` 事件和最终回执，不自动打开浏览器；授权 URL 只用于用户登录。CLI 回调需能到达执行主机的回环端口，远端 CLI 可使用 SSH 端口转发。登录中 Ctrl+C 取消等待。现有非交互任务不会等待标准输入中的授权信息。

## 配置与兼容范围

```json
{
  "mcpServers": {
    "research": {
      "type": "http",
      "url": "https://mcp.example.com/mcp",
      "enabled": false,
      "auth": { "type": "oauth" }
    }
  }
}
```

本版复用 MCP Python SDK 1.30 的资源元数据发现、授权服务器发现、PKCE S256、动态注册及客户端元数据文档流程，也支持用户提供的固定 Client ID。服务仍须公开有效的资源/授权服务器元数据；不宣称能登录任意 OAuth 服务。旧版 SSE、Device Code 和 private_key_jwt 仍未支持。

如果你的服务支持客户端元数据文档，可在高级配置填写 HTTPS 文档地址，或使用 CLI 的 `mcp add ... --client-metadata-url https://your-domain.example/client.json --oauth`。它是你发布的客户端注册资料，不是授权服务器元数据地址；文档及服务端应支持实际的回调 URI。未填写时使用服务公布的动态注册端点。没有内置供应商、默认账号或 Xuloom 公共注册身份。

### 使用已有 Client ID

在图形编辑器中选择 **客户端注册 → 使用已有 Client ID**，填写服务方给出的 Client ID、授权服务器 Issuer 和已登记的回调地址。公开客户端可不填 Secret；只有服务要求时才填写。默认根据已验证的服务元数据选择 Basic 或 POST 客户端认证，PKCE 始终启用。

回调必须是精确的 `http://127.0.0.1:端口/路径` 地址，与服务登记一致。端口被占用时登录失败，不会另选端口或偷偷转为动态注册。路径应为规范的 ASCII URI，不接受网络路径、反斜杠或点路径段。Issuer 不匹配时，在发送客户端凭据前拒绝。

```sh
xuloom mcp add research --cwd /path/to/project --url https://mcp.example.com/mcp --oauth --client-id YOUR_CLIENT_ID --issuer https://auth.example.com --redirect-uri http://127.0.0.1:7777/callback --json
# 若服务需要 Secret，先在终端设置自己的 XULOOM_CLIENT_SECRET，再追加：
# --client-secret-env XULOOM_CLIENT_SECRET
```

CLI 用 `mcp update` 更新已有连接，提供完整的 HTTP 地址和 OAuth 选项；保留显示信息、启停状态与工具超时。Secret 从环境变量读取，不接受命令行中的明文值。高级选项 `--client-auth auto|none|client_secret_basic|client_secret_post` 需与 Secret 及服务声明相容。

Secret 在查询和编辑器中只显示“已保存”；留空保留原值，使用 **清除已保存 Secret** 显式删除。改变 Client ID 或 Issuer 时，不能用一个空框把旧 Secret 自动复制给新身份，必须重新填写或清除。固定客户端 Secret 只以系统加密字段保存于 MCP 配置，授权回执不再复制该值；每次刷新和退出使用当前绑定的配置。手写旧明文文件在显式登录前迁移，旧备份的清理由持有人负责。

OAuth 不能与手工 `Authorization` 请求头或 URL 查询凭据混用。HTTP 仅允许字面回环 IP 的本机测试服务；实际网络服务应使用 HTTPS。授权参考 [MCP Authorization](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)。

## 存储、刷新和退出

- 必须有可用的[系统凭据保护](credentials.md)。OAuth 不接受 `file-only` 或 `plaintext` 保存策略；缺少后端时在网络请求前拒绝登录。Windows 桌面使用 DPAPI；macOS/Linux 安装 `secrets` 可选依赖并准备原生服务，原生平台验证范围见凭据文档。
- 加密回执位于用户数据目录的 `mcp-oauth` 中，按 MCP 配置文件和服务标识隔离；令牌、客户端信息、授权服务器及资源元数据一起加密。绑定还包含目标地址和认证配置，不会把旧令牌静默发给新目标。
- 刷新恢复原始过期时间、资源绑定和已验证的授权服务器，不回退到 MCP 地址的 `/token`。并发刷新使用现有 OS 租约，同一服务不并行消费同一轮换令牌；供应商未返回新刷新令牌时保留原值。
- 原安装 App 和 CLI 共用同一回执；App 在下次任务前发现授权记录改变并重新连接，无需修改模型配置。不会为刷新授权重跑任务。
- 退出先持久清除本地授权，随后向服务公布的撤销端点提出撤销。界面和 JSON 回执区分撤销已接受、未支持和失败；HTTP 成功也不保证该服务将所有派生令牌一并撤销。已经发出的请求无法撤回。
- 取消期间已启动的磁盘发布会在释放写锁前收尾，之后的退出操作优先保留。等待中的旧登录通过版本校验，不能覆盖退出结果。删除服务或修改地址/认证配置也会失效化本地授权。
- 密文不可读时，后台不发送请求。显式重新登录可在新授权成功后替换不可读密文；退出登录可清除损坏回执，原损坏文件移入同目录的 `.invalid-*` 记录以便恢复，不将敏感内容输出到界面。

系统隔离模式仍禁用未受隔离的 MCP 与宿主 OAuth；OAuth 不扩大会话执行权限。当前证据以本机自有 HTTP 授权服务为准，不等于已验证全部实际提供商或物理跨机流程。
