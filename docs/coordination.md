# 并行汇合、逐项流水线与恢复

这些接口复用 `QueryEngine.agents()`、原生／外部／SSH 子代理、FIFO 队列、权限入口和中央会话存储。不增加模型规划轮次、领域验收器或独立调度服务。Auto/Ultra 的数量与预算策略保持原设置。

## 任一与全部

```python
accepted = await engine.agents("spawn", prompt="独立任务", count=3,
    run_in_background=True, request_id="batch-1")
ids = [worker["agent_id"] for worker in accepted["data"]["agents"]]
first = await engine.agents("wait", agent_ids=ids, wait_for="any", timeout_ms=60000)
joined = await engine.agents("wait", agent_ids=ids, wait_for="all", timeout_ms=60000)
```

`condition_met` 表示等待条件满足，**不是任务成功**。完成、失败、取消和中断都会作为已结束返回，仍应检查每个 `status` 和 `receipt.ok`。`finished_agent_ids`、`pending_agent_ids` 保持调用方顺序；`timed_out` 表示仍有本机活跃任务且等待到期。超时不取消剩余任务。无活跃宿主的保存队列返回 `awaiting_resume`，不会假装已完成。

显式传 `wait_for`，即可让文本、JSON、SDK 使用同一语义。为兼容旧调用，未指定时 JSON 默认 `any`，文本默认 `all`。空集合的 `all` 为真、`any` 为假。

## 每项独立推进

保存下面的公开示例为 `pipeline.json`：

```json
{
  "items": [
    {"id": "paper-a", "input": "第一篇资料的 URL 或内容"},
    {"id": "paper-b", "input": "第二篇资料的 URL 或内容"}
  ],
  "stages": [
    {"id": "read", "prompt": "读取资料，保留证据来源。"},
    {"id": "analyze", "prompt": "分析上一阶段证据，标出不确定性。"},
    {"id": "summarize", "prompt": "总结上一阶段分析，保留来源。"}
  ]
}
```

```python
result = await engine.agents("pipeline", run_id="research-1", **spec)
projection = await engine.agents("tree", run_id="research-1")
```

每项按阶段顺序执行；下一阶段取得原始 item 输入及**完整的上一阶段文本结果**。一项读完就可分析，不等其他项读完。阶段可指定注册的 `subagent_type`、`model`、`reasoning_effort`；权限、后端、主机、连接和模型能力仍由现有角色及执行记录约束。未提供角色时采用通用子代理。这里没有额外模型调用来规划或转抄回执。

当前组合调用等待所有可推进的项目结束，再返回；不是脱离会话运行的后台守护进程。协调协程不占子代理并发槽位，实际阶段仍服从 Runtime 的可选并发／队列限制。失败只阻塞该项的后续阶段，其他项可继续；总结果仍明确显示 `blocked`，不会丢成 `null` 或冒充全部成功。SDK 外层 `ok` 仅表示工具调用成功，整体执行完成需检查 `data.runs[0].status == "completed"`。

## CLI 与桌面

```sh
xuloom pipeline start --spec pipeline.json --run-id research-1 --cwd /path/to/work --permission full --json
xuloom pipeline tree --session SESSION_ID --cwd /path/to/work --json
xuloom pipeline resume --session SESSION_ID --run-id research-1 --cwd /path/to/work --permission full --json
```

`start/resume` 使用普通非交互 CLI 权限，默认请求批准；无人交互时需要批准会明确阻塞。`tree` 直接读取中央存档，不初始化模型、不占写入锁、不调用接口。未全部完成的 CLI 返回非零退出码。真实模型执行才消耗供应商额度。

桌面“代理 → 协作运行树”可导入同一 JSON，查看各项／阶段、点击有真实代理 ID 的步骤查看完整对话，或继续未完成步骤。内部 run ID 自动生成。运行树从实际子代理记录投影；不是另一套可随意标记“完成”的状态。普通对话仍可直接调用 `agent_tool` 的 `pipeline/tree` 操作。

## 恢复的边界

- spec、run ID 和摘要先写入既有会话 metadata，再派发。步骤用稳定 request ID 关联现有代理。没有另建数据库或 Job／attempt 状态机。
- 同一 run ID 的 items／stages 不可变；输入不同应创建新的 run ID。
- 复用完成步骤前核对输入签名、原派发内容、成功且已保存的执行回执，以及属于当前会话的完整结果文件。缺失、不同或损坏时停止，不能通过重新调用掩盖证据缺失。
- 重启后只自动恢复明确从未开始的已存队列，以及尚未派发的后续步骤。执行中断、失败或远端结果不明的步骤不会自动重放；先检查子代理历史和已有产物，再通过现有生命周期显式处置。取消后的不确定操作依然需要批准。
- 对已中断代理的显式 followup 若改了该步骤原派发输入，旧流水线会拒绝复用该输出；修改方案使用新 run ID。领域结果是否正确、文件是否满足科研要求仍由调用方或 Skill 验收。
- 父协调器取消会取消并等待其本次启动／恢复的活跃步骤清理；已完成结果和历史保留。不会取消不属于本次协调的无关代理。

实现借鉴了 [agent-core 的 fork/join 与逐项流水线](https://github.com/openJiuwen-ai/agent-core/blob/c8c621b22f1ad5da6acee0ceee33778007c06efb/openjiuwen/agent_teams/workflow/engine/primitives.py)的组合方式，保留 Xuloom 的结构化失败与持久化约束。没有引入 JiuwenSwarm 依赖或其图搜索／自演进服务。验证以本地协议替身和真实应用入口为准，不能据此宣称数小时真实科研工作流已验收。
