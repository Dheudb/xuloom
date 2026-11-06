# 开发与打包

## 开发环境

建议 Python 3.12+、Node.js 24+、Git、ripgrep。使用仓库根目录 `.venv` 管理 Python 依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[mcp]"
cd desktop
npm ci
npm run dev
```

源码桌面端自动寻找 `.venv`，兼容已有 `.venv-mcp`；开发时可用 `XULOOM_PYTHON` 覆盖。
用户界面不提供解释器选择。便携包始终使用其内置运行时，不读取历史解释器设置或开发覆盖值。

## 依赖的唯一来源

Python 依赖和 CLI 入口在 `setup.py`。`requirements.txt` 只引用当前包，
避免维护两份不同的依赖列表。可选 extras：

| Extra | 用途 |
|---|---|
| `mcp` | MCP SDK，Python 3.10+ |
| `browser` | Python Playwright；还需执行 `python -m playwright install chromium` |
| `tokenizers` | 可选的 tiktoken / Transformers Token 计数 |

基础 CLI 支持 Python 3.9；完整桌面构建使用 Python 3.12+。
前端依赖在 `desktop/package.json` 与 `package-lock.json`，使用 `npm ci` 安装。

## 验证命令

发布候选使用[版本与验证流程](release-process.md)：先提交候选，再运行 `python scripts/check_local.py --jobs 3`，生成与该提交和源码哈希对应的结果。性能可用 `python scripts/benchmark_base.py --samples 3` 测量；不要把本地模拟服务的结果当作真实模型任务成功率。

```powershell
# 仓库根目录
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
cd desktop
npm test
npm run build
npm run test:e2e
npm run test:sessions
npm run test:workspace
npm run test:archives
npm run test:models
npm run test:permissions
npm run test:subagents
npm run test:activity
npm run test:mcp
```

Electron 端到端验收默认隐藏窗口，使用本地模拟模型 API 和 MCP 协议测试服务，
不调用真实模型或付费搜索。测试覆盖 Skills、审批、后台代理、项目隔离、会话恢复、
MCP 安装与连接、凭据保留和停用重连。会话测试另覆盖重命名、归档恢复、删除与停止运行中的引擎、
草稿和重启恢复、项目关闭/移除、能力配置说明与示例。截图保存在 `desktop/test-results/`。
工作区测试覆盖首次启动无需选目录、默认目录独立保存与重启恢复、显式项目优先。
归档测试覆盖设置内集中恢复、旧开关迁移、同名会话、搜索、恢复失败重试、缺失目录、项目恢复、草稿保留、反复关闭及重启。
模型测试覆盖一个 API 共用 Key 的多个模型、读取模型列表、跨 API 凭据隔离、切换后保留历史和草稿、运行期间保护以及重启恢复。
权限测试覆盖请求批准、完全访问执行文件/命令/子代理、等待中切换、撤销记忆授权、会话隔离、重启恢复和传输失败时停止引擎。
子代理测试覆盖任务上下文隔离、角色系统提示、多次调用和单次 `count=10` 批量调用的前台/后台并发；测试接口必须收到十个请求后才返回任何结果，验证实际请求并行和 40 个任务的面板状态。
设置 `XULOOM_TEST_DSML=1` 后运行权限或子代理测试，覆盖原样 DSML 文本、分块协议过滤及真实审批/文件/命令/子代理执行。
活动测试覆盖真实工具参数、输出和失败，子代理作用域、审批/拒绝/取消状态、历史恢复，以及面板拖动、键盘调整、复位、宽度保存和窄窗口约束。
`npm run test:turns` 覆盖运行中展开和计时、完成后折叠、过程文字/工具结果保留、每轮隔离、停止/失败以及重启恢复。
本地模型质量、真实搜索源可用性和第三方服务账户权限需要单独验证。

## Windows 便携包

```powershell
cd desktop
# 如构建环境没有仓库 .venv，可以指定构建用 Python 3.12+
$env:XULOOM_BUILD_PYTHON = "C:\path\to\python.exe"
npm run pack
```

`prepare-runtime.py` 从构建机的标准 Windows CPython 中复制解释器和标准库，
通过正常 wheel 安装把 Xuloom 与 MCP 依赖放进 `desktop/build/python/`。
它不复制虚拟环境的绝对路径，也不复制构建机全部 site-packages。
运行时清单记录 Python 版本、依赖版本和源码指纹；变化时会重新生成。

`pack.cjs` 组合 Electron、前端、内置 Python 和引擎，输出到
`desktop/release/Xuloom-win32-x64/`。运行时使用 Python 隔离模式，
不依赖系统 Python、用户 site-packages 或 PYTHONPATH。
完整目录可以移动到其他位置；运行时和第三方许可信息随包保留。

成品验收：

```powershell
$env:XULOOM_TEST_EXECUTABLE = (Resolve-Path "release/Xuloom-win32-x64/Xuloom.exe").Path
npm run test:e2e
npm run test:mcp
```

GitHub Actions 验证 Python 3.9/3.12、前端和 Windows 成品，并上传便携包与测试截图。
目前因 GitHub 账户计费锁定，仅保留手动触发，避免每次推送重复失败通知。
账户解锁后可在 Actions → Desktop workbench → Run workflow 手动运行；
如需恢复自动检查，再为 `.github/workflows/desktop.yml` 添加 `push` 和 `pull_request` 触发。
`desktop/build`、`desktop/release`、虚拟环境、缓存和本地配置不提交 Git。

## 发布已有 Windows 成品

已有安装不要再次运行 `npm run pack`；打包脚本会拒绝覆盖原安装。先构建前端并暂存公开资源：

```powershell
cd desktop
npm run build
cd ..
python desktop/scripts/stage-update.py --validation-receipt desktop/build/local-validation.json
```

使用脚本输出的暂存目录，调用 `desktop/scripts/update-existing.ps1`，指定原安装目录与暂存资源路径。更新器等待应用自然退出，保留旧资源，不结束用户应用。随后用原 EXE 进行成品验收；暂存脚本仅从本地源码构建 wheel，不复制用户 Skills 或配置。

源码应在验证与构建之前固定到候选提交。确认原安装的源码、随包验证、版本与准备发布的标签一致，再归档；发布脚本会拒绝脏来源、过期验证或不匹配的验证哈希：

```powershell
python desktop/scripts/release.py --tag v0.1.33
```

脚本核对引擎、前端和 Electron 代码与源码一致，复用原安装的 EXE 与 Python 运行时，不创建另一份 EXE。
应用仍运行时，也可加 `--staged-resources desktop/build/脚本输出的暂存目录`：先把已验证的新资源合入发行 ZIP，不修改正在运行的安装；本地更新器仍需等待应用退出，成品验证与暂存验证分别记录。
它排除 Python 缓存、本机 wheel 安装来源信息和含构建机路径的 pip CLI 启动器，校验 ZIP 完整性并生成 SHA256 校验文件；
压缩包内 `RELEASE.json` 记录源码提交、版本和构建号。
将同一提交推送并创建版本标签，在 GitHub Release 上传 ZIP 和 `SHA256SUMS.txt`。
正式版使用普通 Release，不勾选 Pre-release；发布说明明确记录验证范围。此流程无需运行 GitHub Actions。
