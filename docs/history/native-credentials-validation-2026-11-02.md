# 可选原生凭据适配器本地验收

日期：2026-11-02。继 Windows DPAPI 批次之后，增加 macOS Keychain / Linux Secret Service 可选适配器。不调用远端模型，不改用户的私人 Skill，不上传或发布。

## 实现范围

- Windows 继续使用 DPAPI，无新增默认依赖。源码安装的 `secrets` extra 提供可选 AES-GCM 依赖，Linux 另用 SecretStorage；不引入凭据插件选择器、独立数据库或常驻进程。
- 原生服务仅保存每个配置目录的随机主密钥，密文与已有原子配置保存路径共用。初始化使用现有操作系统锁，不覆盖已存在但损坏的主密钥。
- 一次文档操作只获取一次系统密钥；获取失败也只尝试一次。单个字段校验失败不影响其他健康字段；不做进程范围的解锁缓存。
- 保存策略支持 `auto / required / plaintext`。选择原生后端后的故障不会隐式降级为明文。已有不可读密文保留，执行入口继续拒绝将其作为 Key 使用。
- Linux 不创建或解锁默认集合，拒绝交互 Prompt/Unlock；连接建立后的方法调用共享五秒期限。D-Bus 库的连接和握手阶段仍由其实现负责，不将这项描述成整个系统调用链绝对不会阻塞。

## 验证边界

测试使用自有临时目录、真实 AES-GCM 和现有跨进程文件锁；内存替身只模拟原生密钥服务的返回与故障。并行初始化、缺失/锁定/损坏主密钥、字段篡改、原子保存失败、原生请求超时与拒绝交互都分别验证。

macOS/Linux 的真实系统用例由 `XULOOM_TEST_NATIVE_KEYRING=1` 显式启用。它们只创建和删除测试自己的原生服务条目；在本机 Windows 跳过，不把替身测试冒充原生平台通过。MCP OAuth、物理跨机、真实供应商长任务和签名更新仍是独立待办。

配置策略和迁移边界见[凭据保护](../credentials.md)；之前的 Windows 原安装与本机凭据迁移证据保留在[上一批验收](credentials-validation-2026-11-02.md)。本轮不重复迁移用户实际配置。

## 已完成的本地检查

- 最终 Python 3.12 全量发现 93 个模块、864 项，分组 439/425 项执行，749 通过、115 条件跳过，零失败/错误；耗时 226.983 / 699.106 秒。最终日志为 `keyring-final-shard-0.log`、`keyring-final-shard-1.log`。新增的两项原生平台跳过与之前 113 项环境条件跳过分别保留，不当作通过。
- Python 3.12 定向 41 项：39 通过、2 个原生平台用例跳过。Python 3.9 同组 41 项：38 通过、3 条件跳过（含可选 MCP SDK 条件）。
- 更新后原安装的隔离 Python 检查 125 项：123 通过、2 原生平台跳过，零错误；17 个 Python 子进程强制 `-I` 并清除源码搜索路径。覆盖凭据、目录、请求、运行连接、媒体生成、桌面桥接、MCP 和输入版本。首轮验收命令误写不存在的 `test_media_bridge`，保留失败日志，改用实际模块后完整重跑，不将失败算作通过。
- Node 72 项全通过，TypeScript/Vite 构建通过；保留既有前端大块体积提示。166 个 Python 模块语法检查通过。
- 源码与原安装的隐藏模型界面流程均通过，覆盖模型/Key 绑定、切换、运行中保护、删除、空目录重启、密文损坏修复及禁止发送旧凭据。
- 实际安装的 Codex / Claude Code / Pi / DSH 共六项本地协议与恢复检查通过，目标全部为自有回环测试服务。

本地原安装构建为 `20261102-180657 / 0.1.22`。176 个 Python/Markdown、62 个前端资源及 18 个 Electron 文件与源码逐字节一致。原启动 EXE SHA-256 仍为 `2529b494f4123f152bce346051f7bd855ef95006424a561953cf17c49a3d1ba8`，稳定路径为 `desktop/release/Xuloom-win32-x64/Xuloom.exe`；保留回滚资源。证明位于忽略目录 `desktop/build/keyring-final-installed.json`。

本轮日志前缀为 `desktop/build/keyring-*`；定向检查为 `system-keyring-final-focused.log`、`system-keyring-python39.log`，桌面构建与 Node 为 `system-keyring-ui-build.log`、`system-keyring-node-tests.log`。`keyring-installed-python.log` 是首轮未通过记录，`keyring-installed-python-final.log` 才是重跑证据。
