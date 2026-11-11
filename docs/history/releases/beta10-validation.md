# beta.10 发布前本地验证

日期：2026-10-28。平台：Windows x64。验证对象：Xuloom `0.1.0-beta.10`，原安装构建 `20261028-155435`。

- 最终 Python 3.12 全量：277 项，**276 通过、1 跳过**（221.843 秒）。包括会话单写者、崩溃恢复、预算、并发、目标、调度、附件、协议和分支存储检查。
- TypeScript / Vite 构建通过；仍有既存的 JS 大包体积提示。
- 桌面 Node 逻辑：**48/48** 通过。
- 源码隐藏窗口检查：新输入框菜单、键盘操作、计划标签、目标配置及本机确定性模型执行、目标取消、1100px 布局、Skills 入口、外观、消息复制和分支，均通过。
- 更新后的同一原 EXE 隐藏窗口检查：输入框与目标、消息操作、MCP、协作档位，均通过。
- 全部只使用本地协议服务与模型替身，不消耗远端模型额度；没有据此宣称真实长任务或跨平台成品已经成熟。

原 EXE 二进制 SHA-256 保持 `2529b494f4123f152bce346051f7bd855ef95006424a561953cf17c49a3d1ba8`，更新应用/Python 包资源，保留回滚。发布压缩包从该安装生成并检查源码一致性，不重新生成另一个 EXE。

公开内容扫描没有发现密钥模式。运行时配置、聊天记录、工作区、私人 Taiyi Skill 和本地发布工具均排除；内置仅 `code-development`、`code-review`、`debug-issue`、`web-research` 四个公开 Skill。个人的外部 Skill 与运行数据位于安装包外，不随公开源代码或发布包上传。

证据位于忽略的 `desktop/build`：`beta10-python312-final.log`、`beta10-node.log`、`beta10-composer-source.log`、`beta10-appearance-source.log`、`beta10-message-actions-source.log`、`beta10-composer-installed.log`、`beta10-message-actions-installed.log`、`beta10-mcp-installed.log`、`beta10-collaboration-installed.log`。相关旧故障与增量验证仍保留在其他历史验证文档中。
