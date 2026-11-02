# 预注册 OAuth 客户端及安全跳转验收

日期：2026-11-02。承接[上一批 OAuth 验收](mcp-oauth-validation-2026-11-02.md)，补齐 App / CLI 的固定客户端入口。Xuloom 仍是本地开发者/爱好者框架，没有新增账户系统、云后台、注册门槛、供应商默认账号、SDK 或调度器。

## 实现与约束

- 用户提供 Client ID、授权服务器 Issuer 和已注册的回调 URI；公开客户端可不提供 Secret。有 Secret 时，默认根据已验证元数据选择 Basic / POST；始终复用原有 PKCE。
- 固定身份只选择资源元数据中与配置匹配的 Issuer，即使它不是列表首项。其他授权服务器、未知认证方式不会触发猜测、注册或秘密复用；预注册流程不调用动态注册端点。
- 固定回调精确绑定 `127.0.0.1` 的指定端口及规范 ASCII URI。端口占用时拒绝，不随机更换。拒绝原始空格、网络路径、反斜杠和点路径段，防止浏览器规范化后无法匹配回调。
- Client Secret 保存到已知 MCP 配置槽，原子保存前必须完成原生加密；只读查询和界面回填不返回原值。空框保留、显式清除，改变身份/Issuer 时不能凭空框复制旧 Secret。
- 固定 Secret 只有 MCP 配置一个权威位置，不复制到 OAuth 回执。刷新与撤销使用当前绑定的配置；动态注册由服务返回的 Secret 仍使用原有加密回执。
- CLI 提供 `mcp add/update`、`--client-id`、`--issuer`、`--redirect-uri`、`--client-secret-env` 与可选认证方式。Secret 不放进命令参数或结果；显式空参数不降级为动态注册。更新保留显示信息、启停状态与超时。
- 标准的同源令牌重定向复用 SDK 的跳转校验；读取可重放的表单体后继续同一交换，不重新开浏览器。资源只允许当前授权边界内的路径，其他地址不接收凭据。
- 授权探测复用 SDK 的资源 GET 安全跳转。GET 为 405 时，仅在明确登录操作中进入元数据发现，不伪造产品连接成功，也不发送 `initialize`；避免仅为登录遗留服务端 MCP 会话。缓存授权后的尾斜杠跳转不会要求第二次浏览器登录。

## 方法

使用实际 MCP SDK 1.30、HTTPX、Windows DPAPI、OS 文件锁及自有回环资源/授权服务器。服务器核验 Client ID、PKCE、固定回调、客户端 Basic/POST 认证和刷新/撤销；包含 Unicode Secret、元数据中第二个 Issuer、端口占用、不可读密文修复、明确清除、明文模式拒绝、CLI 环境变量轮换及空参数拒绝。

隐藏 Electron 流程覆盖自动注册与固定客户端，检测 Secret 不回填、不在 JSON 中出现、空框保留、显式清除及真实 MCP 初始化。增加了对配置面板的真实滚轮检查和自有窗口截图；没有控制用户桌面、登录第三方账号或调用远端模型。原有请求头 MCP 流程单独回归。

最终冻结清单为忽略目录 `desktop/build/oauth-client-source-freeze.json`，覆盖 437 个源码/测试文件；全量结束后再次核对无变动。此前的定向成功不替代最终源码全量与原安装验证。未上传 GitHub，未读取或打包私人 Taiyi Skill。

## 已完成的检查与成品

- 最终 Python 3.12 全量发现 94 个模块、896 项，分组 491/405 项执行：**781 通过、115 条件跳过，零失败/错误**，耗时 776.715 / 267.081 秒。日志为 `oauth-client-final-shard-0.log` 和 `oauth-client-final-shard-1.log`。跳过仍是已有外部 CLI/浏览器/专用 SSH/符号链接门槛及 POSIX 原生凭据条件；本机 32 项 OAuth 用例均实际执行通过。
- 最终定向 87 项全通过，包含实际授权服务器、客户端认证和 CLI 子进程检查。Python 3.9 同组 87 项：48 通过、39 条件跳过；该环境不支持 MCP SDK，明确跳过 OAuth 交换，不用它证明 Python 3.9 可以运行 OAuth。
- Node 72 项全通过，TypeScript/Vite 构建通过；保留既有 JS 大块体积提示。169 个 Python 模块语法检查通过。
- 原安装 Python 157 项：155 通过、2 个 POSIX 原生凭据门槛跳过。主进程从原包导入，22 个 Python 子进程强制 `-I` 并移除源码搜索路径，包含真实 CLI 预注册设置/轮换与空参数拒绝。
- 源码与更新后原 EXE 的自动/预注册 OAuth 图形流程均通过，包括 Secret 不回填、保留、清除、固定回调、实际 MCP 连接和配置面板滚轮。原有市场与手工请求头 MCP 流程也分别通过。截图只属于自有测试窗口。

原安装构建为 `20261102-201857 / 0.1.22`。179 个 Python/Markdown、62 个前端资源及 18 个 Electron 文件与源码逐字节一致；原 EXE SHA-256 保持 `2529b494f4123f152bce346051f7bd855ef95006424a561953cf17c49a3d1ba8`。稳定路径为 `desktop/release/Xuloom-win32-x64/Xuloom.exe`，保留回滚资源，未增加新启动程序。证明为 `desktop/build/oauth-client-final-installed.json`。

最终日志前缀为 `desktop/build/oauth-client-final-*`、`oauth-client-installed-*`，另有 `oauth-client-python39.log`、`oauth-client-node-tests.log` 和 `oauth-client-build.log`。早期跳转测试复现 `RequestNotRead`（HTTPX 的跳转请求体需显式读取），修复后相关真实 HTTP 和全量检查重新执行，不将该失败当作通过。
