# 通用基座源码对照与修复（2026-10-30）

本轮接续 [第一轮框架排查](framework-audit-2026-10-30.md)。目标是减少真实工具错误、结果丢失和机械重试，保持现有执行循环、子代理、会话与权限入口，不增加另一套调度器或用长提示词掩盖实现缺陷。

## 参考范围

参考仓库固定到以下提交；参考源码仅保存在被忽略的本地构建目录，没有打包或复制进 Xuloom。

| 项目 | 固定版本与核对内容 |
| --- | --- |
| Codex | [`c9750d7`](https://github.com/openai/codex/tree/c9750d7f50052b29f53def02b5cf2160ae49a3d9)：[`unified_exec/process.rs`](https://github.com/openai/codex/blob/c9750d7f50052b29f53def02b5cf2160ae49a3d9/codex-rs/core/src/unified_exec/process.rs) 的进程退出、取消和有界输出排空；[`tools/orchestrator.rs`](https://github.com/openai/codex/blob/c9750d7f50052b29f53def02b5cf2160ae49a3d9/codex-rs/core/src/tools/orchestrator.rs) 的执行授权。另核对 [官方安全说明](https://developers.openai.com/codex/security)。 |
| Pi | [`f09d01b`](https://github.com/earendil-works/pi/tree/f09d01b85e0384647fcab6779d35ab9dabe04be3)：`packages/coding-agent/src/core/tools/` 下的 [`file-mutation-queue.ts`](https://github.com/earendil-works/pi/blob/f09d01b85e0384647fcab6779d35ab9dabe04be3/packages/coding-agent/src/core/tools/file-mutation-queue.ts)、`write.ts`、`edit.ts`、`read.ts`，包括真实路径队列、取消检查与图片能力判断。 |
| DSH | DeepSeek Harness [`ce7073d`](https://github.com/deepseek-ai/deepseek-harness/tree/ce7073d51254c26cd63b22a60835b8f948deb447)：[`fs-observation-policy`](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/packages/fs/fs-observation-policy/src/index.ts)、[`atomic-write`](https://github.com/deepseek-ai/deepseek-harness/blob/ce7073d51254c26cd63b22a60835b8f948deb447/packages/util/atomic-write/src/index.ts)、`fs/tool-fs/src/write.ts`，包括每个 agent 的观察版本、无覆盖创建、原子发布与锁。另核对本机安装的公开包 `@deepseek-ai/dsh` 0.2.1-rc.2；未读取它的私人运行目录。 |
| Claude Code | 公开仓库 [`b3f291d`](https://github.com/anthropics/claude-code/tree/b3f291d131924810e2b7713ededc144843a229d5) 主要提供说明、示例与插件，不能据此声称审查了私有执行核心。运行行为参考官方 [工作方式](https://code.claude.com/docs/en/how-claude-code-works)、[检查点](https://code.claude.com/docs/en/checkpointing)、[子代理](https://code.claude.com/docs/en/sub-agents) 文档。 |

这些实现提供可借鉴的机制，不证明任何项目没有 Bug，也不意味着 Xuloom 已与它们逐项功能等同。

## 本轮确认并修复

| 缺陷 | 处理方式 |
| --- | --- |
| 写工具直接打开目标文件再写，编码或磁盘失败可能先破坏原文件 | 复用现有存储原语：先编码、同目录临时文件写入并刷盘，再原子替换；替换已有文件时保留模式位。失败保留原内容并清理临时文件。 |
| 排队前判断“文件不存在”，等到执行时已被其他任务创建，仍可能覆盖 | 在实际写入队列内重新判断；首次创建使用同目录临时文件的无覆盖原子发布，发布竞争失败不替换对方文件。 |
| 工具确认“读过”，却没有核对读后是否被其他任务修改 | 在队列内对照本 agent 最后观察的内容哈希；不匹配时拒绝覆盖并要求重读，不能把过期内容当当前版本。 |
| Notebook 先读取和解析，再仅将最后写入排队，两个编辑仍会丢一个 | 将完整的读、校验、修改、序列化、原子写入放在同一个队列事务中。 |
| 父子 agent 共享读证据，却没有共享物理写队列 | 父子共用文件变更队列；每个 agent 保留自己的文件观察缓存。子代理读过不等于父代理读过。移除没有调用方的父计划覆盖回写代码。 |
| 路径别名可能绕过队列，空闲锁对象长期堆积 | 使用规范化真实路径作为键，空闲的进程内锁采用弱引用回收。 |
| 不同 Xuloom 进程编辑同一文件没有共同锁 | 在原写队列内复用现有 OS 会话锁原语，以真实路径哈希为身份；竞争等待、超时明确失败、取消释放。没有新建第二套锁管理器。 |
| 排队等待时取消，历史仍显示 queued/running | 无论等待本地锁、跨进程锁还是执行回调，取消都记录 cancelled 并释放已持有资源。 |
| REPL 取消可能留下执行中的解释器；输出固定按 UTF-8 解码 | 复用自有进程启动和进程树清理工具，取消等待清理；设置 Python UTF-8 环境并沿用原生输出解码。 |
| 父命令已经退出，但后代继承输出管道，完成等待被拖住 | 将实际退出与输出 EOF 区分，退出后给输出排空设置等待期限；超时返回 `output_incomplete` 和 `output_complete=false`，保存已有输出，不伪装 completed。引擎也按失败回执处理。 |
| 网页工具提前截掉 100,000 字符后的文本 | 完整提取文本交给公共工具结果归档和分页读取，不再提前丢弃尾部；纯文本和 JSON 不当 HTML 清洗。 |
| 网页二进制、过大内容、限流和网络错误缺少明确区别 | 流式读取，显式限制单响应 5 MB，二进制明确拒绝；错误回执区分 HTTP、429、超时和连接故障，并提供是否可重试，不增加内部模型重试循环。 |
| Skill 设置使用固定临时文件名 | 改用现有原子 JSON 写入，避免多个保存操作竞争同一个 `.tmp` 名称。这不等于所有配置修改已经具有跨进程事务隔离。 |

新增回归位于 `tests/test_reference_parity.py`；第一轮生产上下文故障测试继续保留在 `tests/test_framework_integrity.py`。验证覆盖真实文件与本地子进程，不仅检查 mock 返回值。

## 轻量化约束

- 没有新增运行时依赖、Agent 类型、调度状态机或额外模型请求。
- 文件变更复用原写队列和 OS 锁，结果复用公共归档，REPL 复用进程清理，Skill 设置复用原子 JSON。
- 没有新增全局提示词。删除写文件工具中无关的“禁止 Markdown/README”等行为限制；网页工具仅说明实际能力边界。两处工具说明合计减少 64 个字符。
- 源码参考不包含在分发包；私人 Taiyi Skill、个人模型连接、密钥、聊天和工作目录均不属于公开产物。

## 验证记录

- 最终生产代码 Python 3.12 全量：332 项，331 通过、1 跳过（本机符号链接权限）；223 秒。日志为被忽略的 `desktop/build/reference-audit-final-python.log`。
- 桌面逻辑：49 项通过；114 个 Python 模块的语法与未定义名字检查通过；桌面 TypeScript/Vite 构建通过。构建仍提示前端主 chunk 较大，这不是测试失败，本轮未顺带做前端拆包。
- 新增 13 项故障测试先单独通过，再在完整套件运行；软件自带 Python 分别加载暂存资源和更新后的安装资源运行 13 项，也全部通过。
- 源码桌面的文件与框架协作端到端均通过；原 EXE 更新后的两组也通过，包括实际文件写入、增删统计、差异与文件树、最新磁盘内容、权限拒绝、会话隔离、重启持久化，以及同一子代理跨轮/重启、排队消息、模型参数继承、命令 UTF-8 stdin 和完整输出恢复。
- 原安装路径保持不变，资源更新至 beta.18 / 构建 `20261030-112828`，保留回滚资源；未生成另一个启动 EXE，也未强制退出用户应用。
- 中间全量曾通过 331 项，之后增加了管道排空测试和实现，因此重新运行上述最终 332 项，没有以中间结果替代最终验证。静态检查首次因开发环境未安装 `pyflakes` 未能运行，补齐开发依赖后通过；它没有加入运行时依赖。

## 明确边界

1. 文件锁约束使用同一用户数据锁目录的协作进程；任意外部编辑器、其他系统用户、Shell 和任意 Python 代码不受此锁强制约束。观察哈希也不能消除所有非协作写入的检查与提交竞争。
2. 无覆盖原子创建需要文件系统支持硬链接；不支持时明确报错，不降级成有覆盖风险的写入。原子替换保留文件模式位，不承诺保留任意 ACL、扩展属性、所有权和硬链接关系；断电持久性也不同于普通进程失败。
3. 进程退出后输出排空超时只能保证工具不会无限等 EOF，不能据此证明所有主动脱离父进程的后代都已终止。系统级沙箱仍是独立边界。
4. 本轮没有猜测网关别名是否支持视觉，也没有把图片自动 OCR 后冒充原图理解；未知模型能力的显式配置和统一多模态预检仍需独立设计与验收。
5. Claude Code 式文件检查点撤销、任意外部副作用回滚不等同于现有聊天分支/恢复，本轮没有宣称提供这些能力。
6. 本轮使用本地 HTTP 协议服务、故障注入与确定性模型替身，没有消费用户远端模型额度。Windows 本机结果不代替 macOS/Linux 成品、真实模型长任务、限流和数小时科研流程验收。
