# 项目结构

```text
xuloom/
  main.py             CLI 与终端交互入口
  core/               引擎循环、配置、消息、会话与生命周期钩子
  desktop/            桌面 JSON-lines RPC、审批和任务生命周期
  context/            提示词、附件、文件状态与工具结果
  memory/             上下文压缩、Token 估计和会话记忆
  tools/              文件、命令、规划、网页和浏览器等工具
  skills/             Skill 发现、校验、资源访问和加载
  agents/             子代理定义、执行、后台任务与团队协调
  protocols/          MCP 配置/市场/连接，以及 LSP 管理
  security/           审批、工作区路由、Windows LPAC 系统隔离与凭据保护
  features/           浏览器、规划及可选的领域能力与兼容状态
  utils/              Shell 任务、VCR 和结果压缩等辅助代码
desktop/
  electron/           窗口、IPC、引擎进程和运行时选择
  src/                React 工作台
  scripts/            开发服务、运行时准备和 Windows 打包
  tests/              界面状态与隐藏 Electron 端到端测试
tests/                Python 单元/集成测试和本地协议测试服务
examples/             可导入的 Skill 与代理示例
docs/                 使用与开发文档
```

## 一次桌面任务

React → 受限 Electron IPC → 项目会话的引擎进程 → QueryEngine → 模型/工具循环。
引擎把流式文本、工具状态和审批请求以事件返回界面。审批会等待用户响应。
桌面没有直接解析终端输出，也没有向渲染进程开放 Node.js。

CLI 直接使用 QueryEngine。Windows 便携包通过内置 Python 的隔离模式启动引擎，
源码开发模式使用开发环境。各 MCP 连接有独立的异步生命周期，连接失败彼此隔离。

## 配置与产物

| 位置 | 数据 |
|---|---|
| `~/.xuloom/config.json` | 模型连接和用户配置 |
| `<用户数据>/Xuloom/history/<工作目录标识>/` | CLI/桌面共用的会话、索引、子代理和工具输出；Linux 目录名为 `xuloom` |
| `<用户数据>/Xuloom/session-locks/` | 跨进程写者锁；标记文件长期保留，占用随操作系统文件句柄释放 |
| `<项目>/.xuloom/capabilities.json` | 导入的 Skill/Subagent 配置目录 |
| `<项目>/mcp_servers.json` | 项目 MCP 服务与凭据 |
| `<用户数据>/Xuloom/workbench.json` | 桌面项目列表、会话关联与界面设置 |
| `desktop/dist/` | 前端编译结果 |
| `desktop/build/python/` | 打包用的内置 Python 与依赖 |
| `desktop/release/` | Windows 便携应用 |
| `desktop/test-results/` | 隐藏窗口验收截图与测试记录 |

模型配置、会话和构建产物均不进入源码版本控制。
