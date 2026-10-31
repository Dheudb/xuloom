# 当前仓库与分支

桌面顶部显示当前会话工作目录所属的 Git 分支；detached HEAD 显示短提交 ID，有未提交变更时显示小圆点。悬停可查看仓库根目录、提交、暂存/未暂存/未跟踪项数和本地已知的领先/落后数量。空仓库保留分支名并注明尚无提交。非 Git 工作区不显示伪造分支。

切换会话或工作目录后重新查询；任务开始/结束、窗口重新聚焦时刷新，可见窗口每 15 秒检查一次，点击分支也可刷新。长分支名省略显示，完整名称保留在提示中。旧会话的异步响应不能覆盖新会话的标识。

## CLI 与程序调用

```powershell
xuloom git-status --cwd C:\work\project --json
```

交互 CLI 使用 `/git` 或 `/git-status`。非交互输出沿用 JSONL 最终回执，`result.git` 是状态对象；`result.ok` 表示查询流程成功，应另检查 `git.available`。缺少 Git、不是仓库、目录已删除或 Git 拒绝读取有明确状态，不触发模型调用，也不创建任务历史。

桌面 RPC `git.status` 读取当前会话的目录，不接受来自前端的任意替代工作目录，不创建 QueryEngine 或取得会话写者锁。上层 Python 可复用 `xuloom.features.workspace_files.git_status(cwd)`。

主要字段：`available`、`root`、`branch`、`commit`、`detached`、`unborn`、`dirty`、`changed`、`staged`、`unstaged`、`untracked`、`conflicted`、`upstream`、`ahead`、`behind`。无上游比较时 ahead/behind 为 null。未解决的合并冲突单独计入 conflicted，不冒充已暂存文件。未跟踪目录按 Git 的普通状态模式算一项，不将项数冒充递归文件数。工作目录在仓库子目录时，分支与状态摘要对应整个仓库，文件差异面板仍保留原有工作目录范围。

## 只读边界

不执行 fetch、checkout、commit、构建或模型请求；领先/落后数量仅来自本地引用。复用现有 Git 子进程入口，禁用 optional locks 和配置的 fsmonitor 命令，避免状态检查更新索引或执行仓库中的 fsmonitor 脚本。清除会改变查询工作区的继承 Git 环境变量，不改变仓库所有权检查、不写 safe.directory，也不修改用户 Git 配置。查询超时/取消会回收其 Git 子进程。

模型默认上下文仍不自动探测 Git；新增的是用户工作台和显式 CLI 查询入口，不把通用对话改成编程流程。

## 验证

`tests/test_git_status.py` 验证实际仓库的空仓库/提交/混合变更/合并冲突/detached/分支切换/子目录、中文及空格、环境隔离、fsmonitor 不执行、缺少 Git、取消清理、无模型桥接和真实 CLI 进程。补充模拟平台路径验证目录尾部空格不会被 strip 丢失。

`npm run test:git-status` 使用隔离配置、隐藏 Electron 和真实本地 Git，检查顶部标签、尺寸、工作区切换、重启和零模型请求，截图保存在本地 test-results。Windows 证据不替代 Linux/macOS 成品验收。

2026-11-01 本轮源码 Python 3.12 全量 397 项（396 通过、1 跳过）；桌面逻辑 52 项通过，前端构建通过。源码与原安装 EXE 的隐藏 Git 端到端检查均通过，包内 Python 的 Git 状态/差异 11 项通过。原安装资源已更新至构建 `20261101-021845`，EXE 启动路径保持不变。顺便修复页脚版本硬编码及重复 Git 标签样式导致的多余留白。私人 Skill 未参与打包；公开 Release 未替换。
