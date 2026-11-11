# MCP 与权限配置的执行边界检查

本批次只使用隔离临时目录、实际 CLI、隐藏 Electron、测试自有 stdio / 回环 HTTP / SSH 服务；没有调用远端模型、第三方生成服务或私人 Skill。Xuloom 不需要注册账号。

## 复现与修复

- MCP 的 `enabled: "false"` 被当作启用；显式错误的 `type` 可能退回 stdio，或使设置快照抛出异常。入口现在只接受布尔启用值、已支持的字符串类型和对应字段形状。HTTP 地址在 transport 创建前检查原始字符串、端口、空白、userinfo 与片段。
- 单个 Skill 的错误 `mcp_servers.json` 会中断所有服务发现。现按清单／定义隔离错误，用现有设置快照、CLI JSON 和启动日志报告路径及原因；有效兄弟服务继续工作。变更检测包含已挂载 Skill 清单的文件身份，修复后可刷新。
- MCP 编辑、启停的 RPC 不再把错误字段强行转换为布尔值；失败不写文件，启用前重新校验，停用允许保留待修复字段。快照不回传无效行中的原始对象或凭据。
- 权限匹配器中的字符串 `case_sensitive: "false"` 原先变成 `true`，可使显式拒绝规则漏判并落入 Full Access 放行。现在错误布尔、路径、匹配源、模式、未知字段和相互覆盖的比较方式走现有 `config_error` 路径；修好后即生效，不增加提示词、依赖、权限管理器或模型请求。

修复前证据保存在忽略的本地 build 目录：`mcp-configuration-before.log`（10 项，多个子场景失败／报错）、`policy-types-audit.json`（仅授权判断）、`policy-filter-before.log`（7 项中的真实文件工具用例未得到拒绝回执；修复后再明确检查文件不存在）。保留失败证据，不把重跑成功替代首次结果。

## 已完成的局部验收

| 检查 | 结果与证据 |
|---|---|
| MCP 新增边界测试 | 12 项通过；覆盖无效配置不创建 stdio/HTTP transport、有效兄弟真实连接调用、正式 CLI、只读快照、修复与清单变更检测；`tests/test_mcp_configuration.py` |
| 权限、委派、执行与 MCP 配置 | Python 3.12 63 项通过；`policy-filter-final-focused.log` |
| 上述兼容性检查 | Python 3.9 63 项中 61 通过、2 因 MCP SDK 不可用跳过；`policy-filter-python39.log` |
| MCP/OAuth/凭据/发现兼容性 | Python 3.9 78 项中 43 通过、35 条件跳过；`mcp-configuration-python39-final.log`，不宣称 Python 3.9 支持 MCP SDK |
| OAuth 与工具参数授权 | Python 3.12 38 项通过；`mcp-configuration-final-oauth.log` |
| 桌面逻辑与构建 | Node 72 项通过；TypeScript/Vite 构建通过；原有大于 500 kB 的 bundle 提示仍存在 |
| 源码隐藏 UI | 错误清单提示、错误服务配置/移除、修复、有效 MCP 安装、真实连接、工具发现和授权调用通过；`mcp-configuration-source-ui.log`，截图 `mcp-invalid-configuration.png` 已目视检查 |
| 原 EXE 隐藏 UI | 上述 MCP 流程、OAuth 自动与固定客户端/Secret 生命周期通过；`mcp-configuration-installed-ui.log`、`mcp-configuration-installed-oauth-ui.log` |
| 原安装 Python | 首次 MCP 构建 173 项中 171 通过、2 原生 POSIX 条件跳过，23 个自有 Python 子进程强制 `-I`；`mcp-configuration-installed-python.log` |
| 原安装权限补丁 | 最后权限修复后 63 项通过，1 个自有 CLI 子进程强制 `-I`；`config-boundary-installed-python.log` |
| 原 EXE 权限界面 | 两档权限、待审批停止、文件/Shell/子代理执行、撤销权限、会话隔离、重启及传输失败拒绝流程通过；`config-boundary-installed-permissions-ui.log` |

## 全量与最终交付状态

初次全量 908 项发现一项 SSH 原生 worker 用例的启动等待失败：`started.wait(10)` 返回 false，尚未进入收信断言；不是已复现的消息丢失。448 项所在分片 887.889 秒、1 失败、43 跳过；另一分片 460 项通过、72 跳过。首次失败保留在 `mcp-configuration-final-shard-1.log`。用例现同时观察请求事件与真实 worker 结束，失败任务立即显式报错，启动中最多等待 45 秒；单独重新检查通过，测得本机启动等待 10.250 秒，确实超出旧用例任意设置的 10 秒窗口。没有修改产品的超时、排队或收信实现。

权限修复与该用例改进后，最终冻结代码的 **910 项全量：795 通过、115 条件跳过、0 失败／错误**。四个互不重叠分片分别为 219／226／281／184 项，耗时 221.072／440.994／387.209／124.410 秒；日志为 `config-boundary-final-0.log` 至 `config-boundary-final-3.log`。最终 SSH 收信用例也在全量通过，测得启动等待 9.391 秒。跳过仍主要对应可选后端、平台／版本和条件能力，不将其计为已通过的原生验收。

另行重新发现测试并逐项核对四份日志：910 个测试身份及出现次数与当前发现结果完全一致，没有漏跑、重复计数或只重跑单项；证据 `config-boundary-final-coverage.json`。冻结的 438 个生产／测试文件在全量前后哈希一致。Node 72 项、TypeScript/Vite 及原安装局部检查见上表；既有 SDK 弃用提示、五项旧的未使用 import 静态警告及 bundle 大小提示未在本批次处理，不宣称所有 lint／性能警告已清零。

本批次已更新原路径 `desktop/release/Xuloom-win32-x64/Xuloom.exe` 的资源，构建 **`20261102-210440`**，更新回执为 `applied`（2026-11-02 21:05:44 +08:00），旧资源的回滚副本仍存在。最终逐字节核对：179 个 Python/Markdown、62 个前端、18 个 Electron 文件与当前源码一致；证据 `config-boundary-installed.json`。EXE 文件本身的 SHA256 保持 `2529b494f4123f152bce346051f7bd855ef95006424a561953cf17c49a3d1ba8`。

没有新增启动 EXE、上传 GitHub 或替换公开 Release。不将本地模拟协议验收等同于真实模型长任务、物理跨机或原生 macOS/Linux 验收；[持续目标](../product-roadmap.md)仍有效。

## 下一批的只读性能证据

隔离 HOME/配置/数据、禁止非回环联网，分别使用源码 Python 和原安装 `-I -B` 运行帮助与 MCP 列表，两端正常退出。启用 `-X importtime` 且本地回归正在并行运行时，测得约 6.0–6.6 秒；不是闲置机器的性能基准。原安装追踪显示 `xuloom.main → xuloom.core.engine → anthropic` 在帮助路径也被预先导入，SDK 导入占显著部分。`startup-source-audit.json`、`startup-installed-audit.json` 及各自导入日志保存在忽略的 build 目录。

这为后续“只读 CLI 不预加载推理引擎／无关 SDK”的轻量化提供具体入口；本批次尚未修改这条启动路径，不宣称已经提速。
