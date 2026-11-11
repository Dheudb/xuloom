# 目标、非交互与唤醒验证记录

本轮基线 `cefc33f` / beta.8，交付 beta.9，原安装最终构建 `20261028-113137`。所有模型行为使用本机 HTTP/SSE 或受控模型替身，未调用远端模型，未验证 A1.5，也未修改私人 Taiyi Skill。

## 生产路径与验收内容

| 范围 | 检查 |
| --- | --- |
| 目标检查点 | 复用 metadata.goal；明确创建/运行、预算累计、暂停/恢复、取消、阻塞、重启中断；最终保存与回执一致 |
| 目标验收 | 模型声明不替代验收；明确文件检查/可信宿主回调；文字条件等待用户确认；暂停优先于迟到验收回调，宿主通过不能跳过明确失败的文件检查 |
| 防空转 | 三轮没有新工具/产物/worker 证据停止续跑，纯文本换说法不算进展 |
| 预算 | 回合数、活跃时间与输出 Token；主模型和子代理预留共享容量，摘要/记忆也计入；未知/中断用量保守占用预算，旧会话累计用量不侵占新目标预算 |
| 正式 CLI | 真实子进程读取 UTF-8 stdin、纯 JSONL stdout、单一最终回执、自动退出、恢复同一会话及明确退出码 |
| 非交互批准/提问 | 缺少批准不写文件并返回 3；缺少用户输入不调用终端 input、不猜测答案，也不反复调用模型 |
| 跨进程目标控制 | 运行中的 CLI 持有原会话租约；另一进程只投递暂停请求，原写者处理并退出，不抢占写锁 |
| 定时投递 | 原会话、延迟与固定间隔、忙碌/活跃目标排队、错过周期合并、暂停/恢复/取消、两个 worker 不能重复领取 |
| 故障与去重 | 丢失队列确认从会话已保存回执恢复；中断不重放；最终保存失败不能确认完成，重复尝试也不能把未保存的内存完成状态写成队列成功 |
| 桌面产品 | 创建目标、文字条件等待验收、明确确认、定时消息到原会话、一次投递、重启不再次请求模型；目标标题使用可见原始目标 |

目标专项在 `tests/test_goals.py`；投递专项在 `tests/test_schedules.py`；实际 CLI 子进程/本机 SDK 协议在 `tests/test_noninteractive_cli.py`；桌面用例在 `desktop/tests/goals.e2e.cjs`。相关原始日志位于忽略的 `desktop/build/`，可按下述命令重新生成，不上传临时用户数据。

最终状态读取补充检查：活跃写者持有租约时，旁观入口显示 active/owner_busy，而不是误报中断；租约释放后读取恢复状态。`goals-status-final.log` 为 13 项通过。这一处补充之后重新使用最终成品运行 `goals-installed-goals-delivery.log` 与 `goals-installed-cli-delivery.log`；完整 258 项日志是该补充前的全量结果，不将新增第 259 项算作已重跑的全量。

## 可复现命令

最终全量：`goals-regression-python312-final.log` 为 258 项，257 通过、1 跳过，174.581 秒；`goals-regression-python39-final.log` 为 258 项，251 通过、7 跳过，123.606 秒。跳过项是相应解释器不支持的可选运行时检查，不计为通过。前端 TypeScript/Vite 构建通过，保留既有 500 kB chunk 提醒；`goals-node-second.log` 为 48/48。

最终原 EXE 成品 `goals-installed-goals-final.log` 通过，覆盖目标创建、明确验收、原会话定时投递、去重及重启；同一生产引擎的成品 CLI 使用 `goals-installed-cli-final.log` 验证 6 个实际子进程场景。此前同一 beta.9 的 `goals-installed-mcp-first.log`、`goals-installed-resume-first.log`、`goals-installed-shutdown-first.log` 分别验证实际 MCP、停止/继续/编辑以及真实 Windows 检查点占用与退出重试。后续目标预算与投递保存检查修改后，最终完整 Python 回归、目标/调度专项及成品目标/CLI 再次验证。

最终资源更新状态为 applied，构建 `20261028-113137`。原 EXE 的 SHA256 保持 `2529b494f4123f152bce346051f7bd855ef95006424a561953cf17c49a3d1ba8`，回滚资源保留。程序包只含四个公开 Skill。

```powershell
python -m unittest discover -s tests -v
.venv-mcp/Scripts/python.exe -m unittest discover -s tests -v
cd desktop
npm run build
npm test
npm run test:goals
```

成品测试设置 `CODECLAW_TEST_EXECUTABLE` 为现有安装的 CodeClaw.exe，取消 CODECLAW_PYTHON 覆盖。成品 CLI 测试由自带 Python 在 `-I` 下运行，只增加测试目录到 sys.path，不将仓库生产源码加入导入路径；子进程实际加载安装后的 codeclaw。

首轮源码目标、CLI、调度专项和整体验证通过。故障检查继续加入缺少交互输入、暂停与验收竞态、错误旧回执及保存失败重复确认等情况后重新回归，不以早期通过代替最终证据。成品目标用例首轮停在 Playwright 截图阶段并超时；真实业务已进入等待验收，但这轮仍计失败。改用仓库已有的隐藏窗口 capturePage 方法后重跑通过，没有让窗口显式显示，也没有忽略业务断言。

## 边界

这证明的是本地执行协议与已列故障行为，不能外推真实模型科研质量或小时级无人值守稳定性。供应商用量和 max_tokens 需要遵守协议；缺失用量按整份预留计入，不声称精确计费。定时服务必须运行，不提供关机唤醒或隐藏的系统服务。用户关闭后保存中断状态，明确恢复；不自动接管另一个活写者，不终止用户应用。

重复为固定间隔秒数，跨夏令时的当地日历调度、其他平台成品和操作系统级隔离仍未实现或验收，不记为通过。
