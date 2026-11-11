# 参与开发

先阅读[开发说明](docs/development.md)、[项目结构](docs/architecture.md)和 [AGENTS.md](AGENTS.md)（代码布局与提交前检查）。安全问题请按 [SECURITY.md](SECURITY.md) 私密报告。

- 安装依赖使用 Python 虚拟环境与 `npm ci`。
- 引擎改动放在 `xuloom/`，界面和 Electron 改动放在 `desktop/`。
- 更新功能时同步相应使用文档，避免只更新首页宣传。
- 不提交 Key、会话、虚拟环境、依赖目录或构建产物。
- 提交前执行与变更相关的测试；改动桥接、权限或打包时执行端到端验收。
- 提交前运行 `ruff check .`、`mypy` 和全量单元测试；修复类改动附带在修复前失败的回归测试。
- 在 [CHANGELOG.md](CHANGELOG.md) 的“未发布”部分记录用户可见的变化。
- PR 说明应写清具体问题、最终行为和验证结果。

测试用本机模拟服务，不需要真实模型 Key。不要为了测试在默认配置中写入私人凭据。
