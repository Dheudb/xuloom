# 条件满足后跟进

这是现有定时投递器的扩展，不增加另一套任务数据库、Ticket、模型循环或常驻服务。App、正式 CLI 和模型通过同一份 `schedules.json` 登记、暂停、取消和查询。默认提示词不加载长篇协作规则；模型通过已有工具发现入口使用 `schedule_tool`。

适合用户已授权的两种跟进：

- 等指定的本会话子代理全部结束后汇总。执行失败也算结束，但失败与成功分别列出；成功必须有匹配的持久化 transcript 和执行回执。
- 等工作目录里的完成标记文件出现后检查实验产物。标记最大 64 KiB；框架记录路径、大小和 SHA-256，不把文件内容作为新指令，也不把“文件出现”视为实验成功。

普通同轮委派继续使用现有完成保护等待子代理并汇总，无需为同一汇总重复登记跟进；这里用于额外明确授权的持续跟进。本轮本地验证与超时记录见[验收记录](history/followups-validation-2026-11-03.md)。

## 使用

在对话中明确要求“等这几个子代理都结束再汇总”或“等 `experiment.done` 出现后检查结果”。模型可通过工具发现加载 `schedule_tool`，调用 `action="watch"`，提供 `prompt`、稳定的 `request_id`，以及 `worker_ids` 或 `marker_file` 中的一项。子代理不能为主会话登记跟进。

程序入口复用 `schedules.add`：

```python
schedules.add(engine, "检查实验结果，并报告失败或缺失项", due_at=time.time(),
    trigger={"kind": "file", "path": "experiment.done"}, request_id="experiment-42")
schedules.add(engine, "汇总两个子任务，明确缺失的结果", due_at=time.time(),
    trigger={"kind": "workers", "agent_ids": [first_id, second_id]}, request_id="analysis-42")
```

CLI：

```sh
xuloom schedule watch "检查实验结果" --marker-file experiment.done --request-id experiment-42 --session SESSION_ID --cwd /work --json
xuloom schedule watch "汇总子任务" --worker WORKER_1 --worker WORKER_2 --request-id analysis-42 --session SESSION_ID --cwd /work --json
xuloom schedule list --json
xuloom schedule pause --id SCHEDULE_ID --json
xuloom schedule resume --id SCHEDULE_ID --json
xuloom schedule cancel --id SCHEDULE_ID --json
xuloom schedule serve --json
```

App 开启时使用原有轮询；没有 App 时需要运行 `schedule serve`。等待只检查条件，不持有模型请求，不产生周期性模型进度消息。用户实际停止会话后，未开始的跟进会暂停；重新开启进程也保留这一保护。再次执行需显式恢复跟进。执行错误、结果不确定或保存失败不会无限重试。

## 关联、授权与恢复

`request_id` 在同一会话中幂等：同一请求返回原记录，包括已经完成的记录；更换参数复用同一 ID 会拒绝，重新运行应使用新的 ID。子代理绑定其原派发定义，不能把同名 ID 的另一项工作偷偷当作结果。

跟进固定原会话与目录、连接模型和地址、权限、模式、隔离工作区、角色及目标身份；这些变化后停止旧跟进，需重新明确登记。Key 轮换不复制凭据或改变事件身份。CLI 服务使用与登记时相同的权限档位；跟进不会自动提升权限。活跃对话、目标或其他进程持锁时继续排队。暂停或结束的目标不会被跟进自动恢复。

每次跟进沿用原会话 `scheduled_deliveries` 回执：`input_status=delivered` 表示已登记本次投递意图；只有执行返回持久化成功回执后才为 `processed`。`acceptance=execution_only` 始终仅表示本次执行结束，不代表科学验收或用户认可。等待条件没有满足时不产生执行回执。

框架先保存执行意图，再提交原引擎；结束后保存结果，最后确认投递。确认丢失时从会话回执补回，不再次执行；执行中崩溃时保留未知副作用并阻塞，不用重试掩盖不确定性。完成标记随后消失也不妨碍核对已保存的完成回执。

本轮没有加入 GitHub／飞书监听、开放网络 Webhook 或任意进程执行器。远端实验可以在已有 SSH 会话的工作目录写完成标记，由执行端检查；不声称已验证物理跨机长流程。跟进仍使用现有权限和隔离机制，不额外提供操作系统沙箱。
