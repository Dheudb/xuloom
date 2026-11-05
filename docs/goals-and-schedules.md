# 目标模式、非交互 CLI 与定时唤醒

三者复用 QueryEngine、会话检查点、原生子代理、权限和工具恢复日志。Windows 系统执行隔离及其清理恢复见 [隔离说明](queue-and-isolation.md)；其他平台的系统后端仍待实现与验证。

目标完成依据保存在 `accepted_by`：`model_report` 为模型报告，`host_validator` / `declared_file_checks` 为程序或文件检查，`user_or_host` 为用户或接入程序确认。桌面完成条会显示对应含义，展开可读完成说明及检查结果。收起完成条只隐藏显示，不改写完成状态；重新打开会话仍能查看。普通任务不因此新增强制人工审批，也不把模型报告当作独立领域验收。

## 非交互执行

```powershell
xuloom exec "总结给定材料" --cwd C:/work --json
Get-Content -Raw task.txt | xuloom exec - --cwd C:/work --json
xuloom exec "继续上次任务" --cwd C:/work --session SESSION_ID --json
```

源码可用 `python -m xuloom`；便携包可用自带的 `resources/python/python.exe -I -m xuloom exec ...`，不需要系统 Python。交互 CLI 和旧 `-p` 保留；自动退出使用 `exec`。

`--json` 在 stdout 输出 JSONL 事件，最后恰有一个 `type=final` 回执，诊断进入 stderr。最终回执在引擎关闭后输出，关闭保存失败不返回成功。字段保留真实 session_id、persisted、reason。框架执行结束与领域验收是两回事。

默认 `--permission ask` 不读取批准答案，需要确认时拒绝并发出 approval_required；模型提问返回 input_required，不猜测答案。显式 `--permission full` 使用完全访问，但不覆盖显式询问、拒绝或 Plan 限制。

| 退出码 | 含义 |
| --- | --- |
| 0 | 执行和收尾成功，或控制请求已提交 |
| 1 | API、协议、参数或其他执行失败 |
| 2 | CLI 参数解析错误 |
| 3 | 缺少批准或用户输入 |
| 4 | 会话占用或版本冲突 |
| 5 | 持久化失败 |
| 6 | 目标暂停 |
| 7 | 目标受阻、中断或等待验收 |
| 8 | 目标预算耗尽 |
| 130 | 取消或 Ctrl+C |

## 目标模式

桌面在“＋ → 目标”选择模式后直接发送 prompt。默认持续执行；发送前可展开“可选运行上限”，单独限制累计执行秒数、输出 tokens 或轮数，留空的项不设限。上限沿用现有目标预算和检查点，修改目标或恢复不会清零；耗尽后保留结果并停止。时间限制会请求停止当前执行，外部工具清理可能延后；输出限制不是供应商精确金额承诺。目标条可调整目标：先停止当前执行，再保存目标并继续原会话。模型报告完成只表示执行状态，不代表独立领域验收。定时唤醒在“＋”中单独配置。

```powershell
xuloom goal start "完成给定分析" --criterion "保存结果并说明证据" --rounds 20 --seconds 1800 --output-tokens 50000 --cwd C:/work --json
xuloom goal status --session SESSION_ID --cwd C:/work --json
xuloom goal pause --session SESSION_ID --cwd C:/work --json
xuloom goal resume --session SESSION_ID --cwd C:/work --json
xuloom goal cancel --session SESSION_ID --cwd C:/work --json
xuloom goal accept --session SESSION_ID --cwd C:/work --reason "人工检查通过" --json
```

目标保存在现有检查点 metadata.goal。每轮沿用原引擎和已保存的工具结果；未完成时安全续跑。模型通过 goal_report_tool 报告进展、证据、完成声明或具体阻塞。连续三轮没有新工具结果、产物或 worker 证据会阻塞，换说法不清除计数。执行失败不无限重试。

仅文字验收条件进入 awaiting_review，由用户/宿主确认；模型自称完成不会直接通过。CLI 可用 `--checks` 声明自动文件检查，例如 `[{"kind":"file_contains","path":"report.md","text":"RESULT"}]`。支持 file_exists、file_contains，解析后的路径必须留在工作目录，包含检查最多读取 10 MiB UTF-8 文本。它们只证明声明的文件条件，不能代替科学验收。

程序入口为 `goals.create(engine, objective, criteria, ...)`、`await goals.run(engine, async_emit, validator=trusted_async_validator)`、`await goals.control(engine, action, reason)`。验收回调由可信宿主提供，不执行模型生成的代码。`control(resume)` 只更新状态；执行需调用 run。

预算跨恢复累计。输出预算由主模型、原生子代理和摘要/记忆请求共享，发送前预留并限制 max_tokens；用量缺失或请求中断时保守计入整份预留。暂停时间不计入执行时间；崩溃期间未结算的时间和输出保守计入。供应商忽略参数、SDK 内部重试或错误用量无法保证精确账单上限；这不是金额预算。耗尽后需要新目标，不偷偷重置。

另一个 CLI 进程暂停/取消运行中的目标时只投递控制信箱，由持锁写者处理，不抢占锁。control_requested 表示请求已提交，实际状态应再次查询。重启显示 interrupted，明确恢复后才继续；已保存结果复用，不自动重放不确定副作用。

## 定时唤醒

桌面输入框的“＋ → 定时唤醒”可创建延迟或重复消息。聊天可通过工具发现加载 schedule_tool；创建与控制继续遵守权限，子代理不能创建主会话定时消息。等待指定子代理结束或实验完成标记后跟进，见[条件满足后跟进](followups.md)。

```powershell
xuloom schedule add "检查实验结果" --delay 1800 --cwd C:/work --session SESSION_ID --json
xuloom schedule add "每日汇总" --at "2026-10-29T09:00:00+08:00" --interval 86400 --cwd C:/work --session SESSION_ID --json
xuloom schedule list --json
xuloom schedule pause --id SCHEDULE_ID --json
xuloom schedule resume --id SCHEDULE_ID --json
xuloom schedule cancel --id SCHEDULE_ID --json
xuloom schedule serve --json
xuloom schedule serve --once --json
```

绝对时间必须带时区；重复为固定秒数，最低 60 秒，不是带夏令时规则的日历 Cron。错过多个周期合并一次，不集中重放。省略 session 会创建并返回一个真实会话 ID。

任务集中保存在用户数据目录 schedules.json；到期向原会话投递，忙碌、活跃目标或其他写者占用时排队。桌面打开时每 5 秒检查，无桌面可运行 CLI serve。应用与服务均关闭或机器关机时不执行；重新运行后处理到期消息。没有安装隐藏开机服务，空闲等待不调用模型。

队列锁、投递租约及会话锁避免多个 worker 重复领取。投递身份和结果保存进会话检查点，丢失队列确认不重跑。执行中崩溃、存储失败或任务失败会阻塞，检查副作用后再创建新任务，不盲目重试。移动/删除目标不会自动创建替代会话；检查后重新绑定。桌面归档会话不自动唤醒。暂停/取消控制未来投递，不撤销已开始的操作，可使用会话“停止”终止当前工作。

所有本轮模型测试使用本机协议替身；真实模型小时级验收继续暂不运行。私人 Taiyi Skill 保持外部安装，不修改、不上传、不打包。
