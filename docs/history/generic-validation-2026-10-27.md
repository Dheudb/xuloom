# 通用化改造验证记录

基线：`e7ae449` / `v0.1.0-beta.2`。通用化首轮为 beta.3，草稿退出修复为 beta.4，旧独立工作区迁移保护为 beta.5，多实例桌面设置合并为 beta.6，统一退出生命周期为 beta.7，MCP 按需工具加载为 beta.8。本地原安装构建 `20261027-225036`，保留此前回滚资源。下文区分累计结果与每轮证据，完整范围与迁移见 `general-framework.md`。

## 当前累计结果

| 验证 | 结果 | 证据范围 |
| --- | --- | --- |
| Python 3.12.14 全量 | 232 项，231 通过、1 跳过 | 默认策略、执行回执、权限、协议、MCP、会话竞争/存储提交崩溃、子代理、程序接口、上下文恢复及桌面设置并发 |
| Python 3.9.7 全量 | 232 项，225 通过、7 跳过 | 同一套测试；不支持的可选运行时检查明确跳过 |
| 前端构建 | 通过 | TypeScript 与 Vite；存在超过 500 kB 的 chunk 提醒，不是运行时失败 |
| 桌面逻辑测试 | 47/47 | 工作区、布局、状态、目录移动、附件、配置和迁移丢失界面回执后的目录关联恢复 |
| 隐藏桌面工作台 | 通过 | 实际 Electron、SDK、本机模拟 API、权限、Skills、后台代理、重启及项目隔离 |
| 文件与 Git 界面 | 通过 | 非 Git 产物、最新文件、红绿差异、目录树、调整宽度、明确展开的可选 Git 差异 |
| 历史管理、拖拽移动 | 通过 | 复制/重命名/归档/删除、批量操作、草稿、实际文件/命令/子代理工作目录与重启 |
| 桌面权限与停止恢复 | 通过 | 两档审批、显式规则、停止/继续/编辑、恢复快照及会话隔离 |
| 桌面 Skill 选择 | 通过 | 实际目录发现、优先级、完整指令进入请求、键鼠选择与重启 |
| 桌面跨进程所有权 | 通过标准竞争场景 | 真实外部 Python 写者；历史可看，执行/复制/移动/删除拒绝，释放后继续；目录修改和锁已合并到同一 Python 执行器 |
| 存储提交故障 | 通过本地专项 | 真实进程在移动 5 处、复制 4 处、删除 3 处退出，验证恢复与原件保护；附件占用、目标修改和批量占用均单独检查 |
| 旧历史迁移 | 通过本地专项和隐藏桌面测试 | 相关会话占用时迁移拒绝；旧迁移日志不替代目标完整性核对；工作区用户文件保留。旧独立工作区升级新增 7 个真实崩溃位置、16 MB 二进制流式复制、目标修改和缺失源文件失败检查 |
| 索引与新建恢复 | 通过本地专项 | 两个真实进程合并标题/草稿，新建 3 个及独立目录 4 个真实退出位置；迟到更新不复活已删除/移动记录 |
| 桥接进程退出 | 通过真实桥接协议专项 | 检查点写入后、回执前退出，重新连接并重试同一待处理移动，恢复回执且版本不重复增长 |
| 多实例设置 | 通过本地专项与隐藏桌面测试 | 两个真实进程从旧快照并发提交 24 个项目/目录均保留；两个实际 Electron 实例验证项目、属性、独立目录和布局合并。占用设置时本机协议任务正常完成，损坏文件在焦点事件中保留并报告，解除占用重试和重启恢复 |
| 原安装成品 | 工作台、文件、会话占用、独立目录通过 | 实际稳定 EXE、自带 Python 与依赖、临时配置及本机模型服务；中文目录成品失败后修正 UTF-8 协议，最终重跑通过 |
| 统一关闭与退出故障 | 通过 Python 专项与源码/成品隐藏界面 | 并发关闭和等待方取消、保存与资源清理失败重试、关闭后拒绝执行/保存、真实命令进程和 MCP 所属连接释放；实际 Windows 检查点占用时窗口/草稿/原记录保留，解除后退出和重启，无重复模型请求 |
| 更新器回滚 | 单独 1 项通过 | 实际 PowerShell 更新器操作临时的非可执行占位文件，在应用与引擎替换后注入失败；核对旧资源恢复，再用同一包重试成功 |
| 真实 CLI 清空后继续 | 通过 | 独立 CLI 进程及本机 HTTP/SSE；删除原会话后使用新会话回答，不复活旧记录、不将旧内容带入请求 |
| 跨领域确定性验收 | 6/6 | 对话、检索与引用、写作、CSV 计算、显式编程验证、三代理真实生命周期并发；模型决策为替身 |
| 跨领域压缩与继续 | 通过本地契约 | 科研/写作/数据约束、来源和实际工具/worker 状态；空摘要/失败摘要/存档失败保留历史，来源恢复不自动重读文件 |

Python 全量命令、桌面对应命令和验收报告生成命令见 `general-framework.md`。本地原始日志在忽略的 `desktop/build/`，可重跑生成；不会把测试临时用户目录打包为成品。

确定性验收报告明确标注 `live_model_tested=false`。实际工具和磁盘产物按受控用例核对，六例均未观察到工具错误、重复变更或缺失产物。供应商重试、Token 用量和价格缺少证据时为 `null`，不能据此声称费用为零，也不能推出真实模型的研究质量。

## 首轮失败与修正

- 首次全量运行的 5 个错误来自新增长持有锁后，旧测试在临时目录清理前未释放写者；另有 1 个恢复模型参数断言未包含 `persist=false`。测试生命周期与只读恢复断言已更新，没有放宽锁保护。
- Skill 加载断言最初假设固定消息下标，后来按真实 human 消息身份检查。压缩测试曾耗尽有限的 mock 返回序列，修复的是测试控制流，不把该失败算成真实模型问题。
- 新的桌面竞争测试首轮使用了错误的重开参数和未同步 React 的直接 IPC 调用；修正为实际界面重开/发送后通过。界面中立化后的旧标题断言和新增第四个公开 Skill 的数量断言已相应更新。
- 保留失败日志并记录后续通过结果；不把初次失败从历史中抹去。
- 存储执行器改为 Python 后，首轮 Node 测试有 3 项失败：一项是 Windows 规范路径大小写断言，另两项仍在向已不承担操作的 Node 文件函数注入故障。路径断言改用平台路径比较；故障注入移到实际 Python 执行器，并新增 12 个真实进程退出位置。原有真实 Windows 文件占用测试保留，随后桌面逻辑 46/46 通过。

最近全量日志：`generic-framework-regression-eighth.log`（200 项，75.492 秒）、`generic-framework-python39-fourth.log`（200 项，56.727 秒）。移除过渡的借锁 RPC 后，另跑 `session-actor-final-ownership.log`（6 项通过）及 `session-actor-ownership-third.log`（隐藏 Electron 通过）。桌面目录逻辑为 `session-actor-node-third.log`（46/46），迁移和历史界面分别为 `session-actor-storage-first.log`、`session-actor-sessions-first.log`。日志均在忽略的 `desktop/build/` 下。

后续加入索引与独立目录提交后的全量：`generic-framework-regression-ninth.log`（208 项，79.557 秒）、`generic-framework-python39-fifth.log`（208 项，61.664 秒）。桌面逻辑 `session-workspace-node-first.log` 为 46/46；成品工作台、文件和占用日志分别为 `generic-installed-workbench-first.log`、`generic-installed-files-first.log`、`generic-installed-ownership-first.log`。原 EXE SHA256 保持不变，资源更新状态为 `applied`，旧资源留在该暂存更新的 `backup/`。

成品中文默认目录首轮失败时，测试过早读取了旧会话；等待真实会话切换后仍失败，界面报告 `Invalid \\escape`。最终原因是隔离的打包 Python 忽略 `PYTHONIOENCODING`，存储进程以 GBK 解码桌面的 UTF-8 请求。入口显式指定 UTF-8 后，新增中文路径、希腊字母与 emoji 协议测试，并由 `generic-installed-workspace-fourth.log` 证明同一稳定 EXE 下的真实界面流程通过。前两轮失败不算通过。

修复后的全量为 `generic-framework-regression-tenth.log`（209 项，87.068 秒）和 `generic-framework-python39-sixth.log`（209 项，67.908 秒）。另加的更新器专项为 `generic-update-rollback-second.log`，1 项通过；首次专项只因 PowerShell 测试包装器未转发子脚本退出码而失败，包装器修正后保留原故障注入并通过。

## 未完成与不可外推

beta.3 已发布为提交 `54437d2`，ZIP SHA256 为 `a7f6a100b32032c8eb3c03da50c5a6f2cbcb5967d1d791e7b007dc2300427c10`。262 个源码文件和 8032 个归档文件隐私扫描均无发现，GitHub 资产摘要与本地一致。beta.4 为 `8ff1855`，ZIP SHA256 为 `921353038c27ab05e9c76403ed0b7d65652b724ea5398939300367f3ce122097`，同样已发布并核对资产。

beta.4 的退出草稿修复：`draft-exit-third.log` 使用真实跨进程索引占用验证关闭/重载被阻止、内容保留、解除占用后立即退出并在重启恢复。首轮测试缺少有界等待，停止隔离实例后补充了超时；进一步诊断确认同步 IPC 提前返回 `false`，即便存储恢复并写入成功也无法退出。修复为操作结束后单次回复后通过，未改成忽略保存失败。正常工作台 `draft-exit-workbench-first.log`、会话管理 `draft-exit-sessions-first.log` 和桌面逻辑 `draft-exit-node-first.log`（46/46）均通过。原安装成品的故障测试 `draft-exit-installed-first.log` 与正常工作台 `draft-exit-installed-workbench-first.log` 均通过，自带运行时、构建 `20261027-202137`。Python 核心未改动，未重复运行全量 Python 套件。新用例加入手动 CI 的源码与 Windows 成品步骤，未恢复自动触发。

beta.5 旧工作区升级修复的全量日志：`legacy-regression-python312-first.log`（214 项，91.528 秒）和 `legacy-regression-python39-first.log`（214 项，72.988 秒）；专项 `legacy-storage-second.log` 21 项、桌面逻辑 `legacy-upgrade-node-second.log` 47 项通过。7 个崩溃位置覆盖意图保存、锁指针保存、配置写入、产物复制、检查点发布、原检查点移除及提交完成；启动扫描恢复待提交记录，准备阶段不创建工作区，已提交但丢失回复的目录关联从检查点恢复。16 MB 二进制文件通过分块复制和完整哈希校验，改变过的目标保留，缺失源文件明确失败。源码隐藏界面 `legacy-storage-e2e-first.log`、原 EXE 成品 `legacy-installed-storage-first.log`、`legacy-installed-draft-first.log` 和 `legacy-installed-workspace-first.log` 已通过。

beta.6 设置合并的 Python 全量日志为 `preferences-regression-python312-first.log`（221 项，93.235 秒）和 `preferences-regression-python39-first.log`（221 项，74.104 秒）；专项 `preferences-python-first.log` 7 项、桌面逻辑 `preferences-node-first.log` 47 项通过。双实例源码验收 `preferences-e2e-fourth.log` 验证实际生产入口、两个不同浏览器 profile 共享同一数据目录、6 个项目添加、2 个独立工作目录、属性及宽度合并、移除不复活、设置锁占用时任务继续、损坏设置在焦点事件中不导致退出，以及重启。首轮在业务断言结束后因测试清理误用已经关闭的 Playwright 对象失败；通过已确认的隔离 profile 端口正常关闭该测试窗口，修正测试清理后重跑，未强制结束用户应用。源码草稿退出 `preferences-draft-first.log` 与旧工作区迁移 `preferences-storage-first.log` 均通过。同一原 EXE 成品及自带 Python 的 `preferences-installed-first.log`、`preferences-installed-workspace-first.log` 和 `preferences-installed-draft-first.log` 均通过，资源更新状态 `applied`，构建 `20261027-213212`，旧资源保留用于回滚。

beta.7 统一关闭生命周期的最新全量日志为 `shutdown-regression-python312-third.log`（228 项，97.994 秒，227 通过、1 跳过）和 `shutdown-regression-python39-third.log`（228 项，78.130 秒，222 通过、6 跳过）。新增关闭专项 7 项；执行契约专项 16 项检查工具保存失败时没有未读取的异步异常。删除两处未使用的末尾流式工具聚合 Future，保留实际需要等待的中间分支。首轮关闭专项因测试清理顺序未释放竞争者租约而失败；全量首轮另暴露两个并行测试使用相同全局空草稿 ID 的隔离问题。修正临时数据目录和测试生命周期后重跑，未削弱真实 OS 锁。

源码 `shutdown-e2e-first.log` 用真实 Windows 检查点句柄禁止原子替换，验证退出失败保留窗口/草稿/原记录、释放后重试、最终版本增长和重启恢复，模型请求仅一次。原 EXE 与自带 Python 的 `shutdown-installed-shutdown-first.log`、`shutdown-installed-mcp-first.log`、`shutdown-installed-resume-first.log` 和 `shutdown-installed-draft-exit-first.log` 全部通过；分别覆盖检查点退出失败重试、真实 MCP 连接与停用重载、暂停/继续/编辑/重启、实际索引占用与草稿退出恢复。桌面逻辑 `shutdown-node-first.log` 47/47、前端构建通过。资源更新 `applied`，构建 `20261027-221722`，原 EXE 哈希未改变，旧资源保留。

beta.8 补齐 MCP 按需加载：CLI 与桌面连接后的工具进入现有潜在目录，发现并加载后才出现在下一次模型请求的详细参数中。专项 `discovery-python-first.log` 4 项通过，覆盖实际 CLI 入口、显式询问、拒绝和 Plan 限制、活动/潜在名称冲突，以及真实 MCP 连接的重连调用和停用清除。全量 `discovery-regression-python312-second.log` 为 232 项（231 通过、1 跳过，106.059 秒）；`discovery-regression-python39-first.log` 为 232 项（225 通过、7 跳过，81.385 秒）。3.12 首轮因旧 MCP 测试替身未包含潜在目录和注册接口而失败；该用例改用真实注册方法并按新的按需加载协议断言，真实 MCP 专项和全量重跑通过。

源码隐藏界面 `discovery-mcp-source-first.log` 验证初次请求不含 MCP 详细工具、发现后加载参数、实际调用仍请求批准、凭据脱敏和停用后继续。成品使用同一原 EXE 与自带 Python，证据为 `discovery-installed-mcp-first.log` 和 `discovery-installed-shutdown-first.log`，均已通过。桌面逻辑与前端源码未改动，47/47 和构建证据沿用 beta.7；不伪称本轮重新运行。构建 `20261027-225036`，资源更新 `applied`，保留此前回滚资源。

1. 目录变更、索引写入、新独立目录创建、旧独立工作区升级及桌面设置合并已使用现有 OS 锁与存储入口。相同设置字段按实际提交顺序保存；活动会话和打开列表成对表示最近保存窗口，不跨窗口聚合，不合并会话执行检查点。
2. 真实模型的跨领域任务与小时级运行、真实限流/断网及成本验收尚未完成。用户选择先完成本地验收，暂不调用远端模型。A1.5 排除。
3. 每版发布前均核对源码、安装资源、标签及 Release 资产；历史版本记录以各自 Release 为准。原安装仍使用同一 EXE，回滚资源保留。当前验证不能外推到旧版不遵守锁协议的应用或任意外部工具副作用。
4. Linux/macOS 成品与操作系统级隔离保留为独立里程碑，不能从 Windows 或路径重定向推断其可靠性。

本轮没有修改私人 Taiyi Skill，没有改变手动 CI 策略，也没有上传配置、密钥、会话或个人工作目录。发布前仍需针对最终源码与最终程序包分别做隐私审计。
