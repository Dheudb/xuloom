# Scaling：显式开启的 SSH 蜂群

Scaling 是独立工作台，不属于 Solo／Auto／Ultra，也不会在普通会话中自动开启。本机总主 agent 规划方向；本机可选参与执行，也可配置多台 SSH 主机。每个执行节点按容量建立独立组长，由组长直接拆分、带领 worker 并汇总证据，最后交给总主 agent 复核并决定继续、完成或说明阻塞。

这是高用量功能。每次创建运行前都要确认用量，选择 SSH 节点和工具权限；仅进入界面或保存配置不会调用模型。关闭界面、退出桌面或退出 CLI 不等于取消已接受的蜂群任务。

**实验阶段／开发测试版。** 功能仍在开发与本地验证，不视为生产级集群调度。本机参与和分组主会话需要执行节点更新到 v0.1.30 或兼容版本；新运行创建前检查对应能力，旧节点不支持时明确拒绝，不静默采用不同层级。已保存的旧运行保留原来的调度结构。

## 桌面使用

1. 点击左侧 **Scaling 蜂群**，阅读用量说明后进入配置。可添加本机、SSH 节点，或两者一起参与。
2. **添加本机**：无需 SSH，配置 worker 容量、每组上限、预留内存和 API／模型。保存后勾选才参与本次运行；不勾选本机时只在本机运行总主 agent。仅允许一个本机执行配置，避免重复计算容量。
3. **配置 SSH**：使用已有 SSH 连接，或新增连接。系统 OpenSSH 负责认证与主机密钥验证；先在终端确认能正常登录。远端须安装兼容版本的 Xuloom。默认沿用本机当前 API 配置：选中的凭据经已认证 SSH 传送、按远端系统凭据保护策略保存，推理由远端直接请求供应商，不经本机代理。私人 Skill、其他凭据和项目文件不会打包传送。也可选择远端自己的模型配置。
4. **添加节点**：选择 SSH 连接、worker 数量上限和预留内存比例。默认按主机资源保守调整并发；测试节点可查看 CPU、内存建议及实际模型。多个别名指向同一执行主机时，不会重复计算容量，包括本机与 SSH 别名重复指向同一执行环境的情形。
5. 可在节点的高级项选择原生 Xuloom 或已安装的 Codex、Claude、Pi、DSH worker 执行器；组长复用原生 Xuloom 会话。节点可继承本机当前连接、选择其他已保存的本机连接／模型池，SSH 节点还可使用该主机上已有的连接；同一节点可选择多个模型。新增地址和 Key 复用“设置 → 模型连接”。外部程序与协议须兼容，未安装或不兼容会明确失败，没有静默替代。
6. 勾选参与节点，填写目标并确认高用量，然后启动。工具权限仍为“请求批准”或“完全访问”；Scaling 不会自动扩大权限。
7. 在运行记录查看节点健康、组长、排队／执行状态及工作结果。点击组长可展开 worker 和分组汇总；点击任务可看原始回答、原生会话 ID 与工作目录。权限请求在工作台中明确批准或拒绝。

默认每组最多 50 个 worker，组数为容量向上除以 50，任务数均匀分配、组间最多相差 1：51 → 25＋26；60 → 30＋30；100 → 50＋50；101 → 33＋34＋34。本机设 100 时，对应总主 agent → 2 个组长 → 100 个 worker；SSH 主机设 100 时，该主机有 2 个独立 Xuloom 组长会话，各带 50 个 worker。组长不计入 worker 容量，但会消耗模型额度与主机资源；数量是工作容量，不保证所有进程／模型请求同时运行。

**暂停派发**停止新工作分配；已经开始的 worker 可以完成当前执行。**取消运行**向节点发送停止请求并等待结果确认；断连时保留“存活未确认”，不能把本机点击取消当作远端已停止。失败／执行器中断后，可查看旧结果、确认副作用后重试；新尝试有新身份，旧记录保留。运输结果未知的任务不允许盲目重试或换主机重派。

## 运行中与主 agent 对话

在运行详情的 **与主 agent 对话** 中输入消息。Enter 发送、Shift+Enter 换行；可询问进展、讨论结果，或明确要求改变目标／探索方向。回复使用本机主协调者已有的原生会话与历史，不另起一个无上下文的聊天。消息会显示排队、回复中、应用中、已应用或失败；失败不冒充已经改变远端工作。

- 普通问答不重启 worker、不改目标。为保持主会话单写者，需等待该主 agent 当前回合落盘，再处理用户消息；问答期间暂缓新探索派发。
- 明确调整方向时，主模型提供修订目标和分组计划。运行时请求停止旧的未完成任务，确认其结束后创建下一轮；已完成的产物、执行回执和历史保留，不自动撤销副作用。SSH 断开时保持“应用中／等待节点”，重连确认前不宣布改向成功，也不重复派发旧任务。
- 暂停中的运行可以问答，问答不会恢复探索；在暂停状态调整方向后仍保持暂停。已完成的运行可继续讨论，明确改向会开启下一轮；取消中的运行与已取消的运行不再接收新消息。
- 消息身份持久化，同一身份重复提交不重复执行；控制进程重启后继续处理已保存的待办。对话不允许自行提高已确认容量、改变权限或更换冻结的 API 绑定。
- 同一连接有多个并发槽位时，为本机主协调者保留一个；只有一个槽位时，需节点确认模型准入暂停、已准入请求结束，才能临时借给本次对话。该离线配额仍被原运行占有，不会分配给其他运行。单槽位节点断线且持有配额时，需要等待恢复，不能安全地强行收回额度。

## CLI 使用同一控制端

```sh
# 本机参与：无需 SSH，可只选本机，也可与远端一起选
xuloom scaling save-node local --local --max-agents 100 --group-width 50
# 先按 remote 命令配置 SSH 连接 lab；默认继承本机当前 API，由远端直接请求
xuloom scaling save-node lab-node --ssh lab --max-agents 50
# 也可为节点选择其他本机 API 与模型；重复 --pool 形成异构模型池
xuloom scaling save-node lab-node --ssh lab --max-agents 1000 --group-width 50 --api-source local --pool lab-api model-a --pool other-api model-b
# 使用远端自己的已保存 API 时：--api-source remote --connection REMOTE_CONNECTION --model MODEL
xuloom scaling check lab-node --json
xuloom scaling nodes --json

# 多节点可重复 --node；不设置 --target 时使用所选节点容量之和
xuloom scaling start --node lab-node --goal "研究目标、约束与所需成果" --accept-high-usage --request-id research-001 --json
# 两者一起参与
xuloom scaling start --node local --node lab-node --goal "共同探索目标" --accept-high-usage --json
xuloom scaling list --json
xuloom scaling status RUN_ID --json
xuloom scaling watch RUN_ID --json
xuloom scaling tasks RUN_ID --offset 0 --json
xuloom scaling result RUN_ID --task TASK_ID --json
xuloom scaling children RUN_ID --task NODE_PRIMARY_TASK_ID --json
xuloom scaling child_result RUN_ID --task NODE_PRIMARY_TASK_ID --child CHILD_TASK_ID --json
xuloom scaling events RUN_ID --cursor 0 --json
xuloom scaling questions RUN_ID --json
xuloom scaling message RUN_ID --text "先总结当前证据，不改变探索方向" --request-id progress-001 --json
xuloom scaling messages RUN_ID --json
xuloom scaling message RUN_ID --text "改变方向：优先核查前两个方案的反例" --request-id redirect-001 --json
xuloom scaling answer RUN_ID --question QUESTION_ID --choice allow --json
xuloom scaling pause RUN_ID --json
xuloom scaling resume RUN_ID --json
xuloom scaling cancel RUN_ID --json
xuloom scaling retry RUN_ID --task TASK_ID --confirm-side-effects --json
```

`--request-id` 是创建意图的稳定身份：同一请求丢失回执后重复提交只返回原运行；更换目标却复用身份会拒绝。它不是模型或 API Key。CLI `watch` 只是观察，Ctrl+C 结束观察不会取消后台运行。JSON 的 `scaling.status` 才是蜂群状态；命令退出成功仅表示控制请求成功。

## 执行与存储

- 控制端与每台主机的节点服务按需启动，使用现有 OpenSSH JSONL 复用连接。不是为每个 worker 建一个 SSH／Python 服务，也没有 Redis、消息队列或新模型 SDK 依赖。节点服务仅监听带随机凭据的本机回环端口；远端通过 SSH 访问，凭据留在所在主机。
- 待执行项是 SQLite WAL 中的描述；实际进入容量的任务才创建引擎／外部执行器。逐步升高并发，压力升高时收缩新准入；多个运行共享主机容量。集群控制端为每个节点预分配供应商并发／速率额度，节点在分配内本地排队，不为每次推理等待本机回复。断连后仍保留该额度，只有确认节点任务结束才释放；同一 Key／端点的多模型共享同一份额度。该机制管理 Scaling，自行在其他应用使用同一 Key 的请求不受它控制。主机上复用现有供应商限流与重试处理。
- 选择 Codex、Pi 等外部执行器时，每个实际执行的 worker 会启动独立的后台 CLI，完成或取消后清理；不是打开多个桌面窗口。API 限流控制模型请求，不等于 CLI 进程数：CLI 可能已启动并等待模型额度。主机资源自适应与节点容量控制执行准入，外部程序还可能创建辅助进程。原生 `native` 复用节点服务内的异步引擎实例，避免每个 worker 启动一套外部 CLI。
- 本机主协调者生成独立方向，代码按各节点容量均匀分组；以 1000 个 worker 为例，默认 20 组、每组 50 个。组长用同一个原生会话进行展开、汇总和分组复核，直接管理 worker，不再额外启动一层节点主模型。每组 worker 上限可配置；默认每次最多拆分／汇总 50 项，较宽的组分批推进。250、1000 不是总容量上限。组长描述先落盘，实际接受和模型推理由资源及 API 额度约束；不会仅因设置了巨大数值就直接启动同等数量的引擎。下一轮探索由总主模型决定，不用复制相同提示词凑数量。
- 总复核必须等待本轮已派发的任务及组内汇总落定。长结果完整保存；协调者获得明确标注的片段，可通过仅在 Scaling 中提供的 `scaling_evidence` 分页读取原始结果。汇总不是悄悄截断后伪装全文。
- 控制索引位于平台数据目录的 `scaling/index.sqlite3`，远端执行索引为 `scaling/node.sqlite3`，节点自治调度索引为 `scaling/node-leaders.sqlite3`，本机协调执行索引为 `scaling/primary.sqlite3`。对话本体继续存现有集中式 `history/`，不在 `.xuloom` 复制一套聊天记录。
- 每个执行有独立产物目录 `scaling/workspaces/RUN_ID/TASK_ID`。SSH 连接指定的工作目录作为执行主机上的参考目录；该目录中的 Skills、MCP 配置在执行主机读取，文件不会自动复制到 worker 目录或本机。跨主机产物不会自动合并进项目；结果记录保留其主机、工作目录和原生历史身份。
- SSH 网关断开后，节点主 agent 继续已接受任务的规划、推理、worker 调度与汇总；明确的人类审批与尚未送达的改向请求需等待连接恢复。重新连接用原任务 ID 核对；只有节点进程实际退出后，原来的 running 才会记为 interrupted，不自动重放未知副作用。旧任务及回执保留，明确改向另开一轮。

## 当前证明范围

本轮只做本地确定性测试、实际 SDK 到本地 HTTP/SSE、加密 SSH 回环及隐藏 Electron 测试，不调用真实模型或外部机器。1 万条排队记录和千级分组结构的测试不是千台机器／千个真实模型同时运行的性能结论。

具体轮次、首次失败及复测结果见 [本地验证记录](scaling-validation-2026-11-04.md)。供应商请求额度继续使用已有的 [`model_request_limits`](models.md) 配置（默认并发 4）；节点智能体上限不会自动提高供应商额度。集群预分配只覆盖 Scaling，不统计其他普通会话或其他应用对同一账户的请求。

运行中对话、改向、持久化和成品更新的验证见 [对话专项记录](scaling-conversation-validation-2026-11-04.md)。

本机参与、均衡分组与组长主会话的验证见 [本机节点专项记录](scaling-local-validation-2026-11-04.md)。

CPU／内存观测是准入估算，不能硬限制任意 Shell、浏览器、构建或本地模型的资源使用，也不替代操作系统沙箱。配置容量不等于供应商承载量；API 上限、内存压力、有效独立工作量都会减少实际运行数量。完成显示明确为“模型报告完成”，不冒充科学验收、程序检查通过或用户验收。未做真实跨机断网、千级真实模型、外部执行器大规模混合运行和 Linux／macOS 桌面验收。
