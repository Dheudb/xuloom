# 配置凭据保护
Windows 端保存配置时，模型 API Key、生成模型 API Key，以及 MCP 的环境变量、请求头、带查询参数的 URL 和预注册 OAuth Client Secret，使用当前用户的原生 DPAPI 加密。敏感字段与原配置一起原子保存；没有第二个凭据数据库、主密码、后台服务或额外密码学依赖。调用 API 时才使用解密后的字符串；只读流控设置不会顺带解密所有凭据。

依据：[Microsoft CryptProtectData](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata)、[CryptUnprotectData](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptunprotectdata)。实现使用当前用户保护及禁止弹窗标志，不使用机器范围保护。配置中看到 `$xuloom_secret` 与 Base64 数据是加密后的字段，不是另一个 API Key。

## 迁移和修复

旧明文配置仍能读取。只读查询不修改文件；下次保存、切换模型或更新配置时，会保护相应配置文件中的已有敏感字段。也可以显式运行：

```powershell
xuloom credentials status --json
xuloom credentials migrate --json
xuloom credentials migrate --cwd C:\work\project --json
```

带 `--cwd` 会同时处理该项目 `.xuloom/mcp_servers.json`；不会创建缺失的 MCP 配置或扫描其他项目。MCP 修改与迁移共用跨进程写入锁，实际保存仍用已有原子文件机制。

受保护字段绑定配置文件位置及字段位置。通常应由原用户在原机器使用；手动挪动密文到另一文件、另一连接或另一台机器后，可能无法解密。模型名和地址仍可查看，设置页会明确提示；重新填写 Key 即可修复，也可删除不再使用的连接。MCP 环境变量和请求头可逐字段重新填写。无可靠凭据时，连接、模型发现和生成请求在发出前拒绝，不回退到其他连接的 Key。

CLI 可以从环境变量替换指定聊天模型连接的 Key，不把值写进命令参数或输出：

```powershell
# 先用你自己的终端方式设置 XULOOM_NEW_KEY，然后执行：
xuloom credentials set-model-key --connection my-api --key-env XULOOM_NEW_KEY --json
```

`my-api` 是已有模型连接 ID，可从 `credentials status` 的公开配置中读取。生成模型继续使用已有 `xuloom media` 配置入口。CLI 明确设置的 API 环境变量仍按原优先级覆盖保存配置，这是用户主动配置，不是解密失败后自动挑选其他凭据。

## 保存和备份边界

- 加密或原子替换失败时，原配置保持不变；临时文件不含新明文凭据。损坏或未来版本的密文会保留，可继续查看配置和修复，不会按空配置覆盖。
- 本轮保护明确的凭据字段。不会将扩展自有的数据或 JSON Schema 中同名属性当作凭据改写。命令参数、自定义未知字段和模型 API URL 中自行嵌入的认证信息不在此覆盖范围；使用专门的 Key、MCP 环境变量或请求头字段。
- Windows 当前用户的加密不是独立操作系统沙箱；同一用户下获完全访问授权的代码、管理员、运行时内存及旧备份仍有各自风险。没有承诺阻止已授权的宿主代码读取当前用户凭据。
- 加密配置的备份也依赖原用户及路径上下文。跨机器迁移保留模型/服务信息，并在目标机器重新填写 Key。共享项目应避免提交含个人凭据的配置，密文也不适合当作通用发布配置。
- 旧版本没有受保护字段的读取协议；降级前应准备好重新填写连接凭据，不会为兼容旧版本另存一份明文副本。
- macOS/Linux 可选原生适配器见下节；本机 Windows 验收不能替代它们的真实系统验收。[MCP OAuth](mcp-oauth.md) 的加密授权回执复用同一凭据保护，不允许明文降级。

## macOS / Linux 可选原生存储

源码安装时运行 `python -m pip install '.[secrets]'`。macOS 使用当前用户的 Keychain；Linux 需要运行中的 Secret Service 及已解锁的默认集合。无桌面密钥服务的服务器可用环境变量提供模型 Key。

原生服务只保存每个配置目录的一把随机 256 位密钥。凭据使用 `cryptography` 的 AES-GCM 加密后仍存入原有配置文件，绑定文件和字段位置，不另建凭据数据库。一次配置读写只查询一次原生密钥，不在进程范围缓存解锁状态。初始化沿用操作系统文件锁；已有密钥损坏或无法读取时不替换它。

`XULOOM_CREDENTIAL_STORAGE` 控制保存策略：

| 值 | 行为 |
| --- | --- |
| `auto`（默认） | Windows 用 DPAPI；macOS/Linux 安装可选依赖后启用原生服务。未安装时维持 `file-only`，设置页明确显示。 |
| `required` | 必须有原生后端；依赖缺失、服务锁定或不可用时拒绝保存敏感字段。 |
| `plaintext` | 用户显式选择文件明文保存。仍能读取已有密文，不自动将不可读密文改成空值。 |

已选择原生后端后，运行时出错不会降级保存明文。程序不创建或自动解锁 Linux 密钥集合，也不执行原生交互授权提示；请在系统中先完成授权。连接建立后的 Secret Service 请求有共享等待期限；D-Bus 库的连接及握手阶段仍由其自身实现负责。macOS 使用禁止授权弹窗的原生查询。

删除系统密钥会导致该配置目录下已有密文不可读，需重新填写凭据。仅复制配置文件不能迁移密钥。macOS/Linux 有独立的 opt-in 原生验收入口 `XULOOM_TEST_NATIVE_KEYRING=1`；本轮 Windows 只验证 AES-GCM、故障处理和本地替身契约，不宣称两个系统已实测通过。
