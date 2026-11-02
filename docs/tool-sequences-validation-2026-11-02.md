# 条件式工具续跑：本地验收

日期：2026-11-02。原安装版本保持 0.1.22，资源构建 `20261102-215313`。本轮没有调用远端模型、发布 GitHub 或读取、打包私人 Taiyi Skill。

## 实现

按需发现的 `tool_sequence` 接收已确定的工具与字面参数；仅当先前完整结构化回执满足条件，才通过现有授权执行器推进下一步。条件未知、失败、取消、目标暂停、新输入、执行范围或工具绑定变化都会停止续跑。每一步继续使用原生权限、隔离、执行记录及结果保存，不新增模型服务、依赖包或独立调度状态机。

实现借鉴 [HarnessMM c8f1994](https://github.com/YuhuaJiang2002/HarnessMM/tree/c8f19947e59feaa8103c05155bd79ef3eb084730) 的字面动作与回执条件，参考 [DeepSeek Super-agent 原型 6337fc8](https://github.com/deepseek-ai/dsh-superagent-prototype/tree/6337fc823ce20251c8de8085948165c81aaa701e) 对输入送达、消费和执行归属的区分。未运行其研究实验或导入其运行服务，不宣称复现论文效果。使用方法与限制见[工具续跑](tool-sequences.md)。

## 最终代码验证

| 检查 | 结果 |
|---|---|
| Python 3.12 全量 | 927 项：812 通过、115 条件跳过、0 失败 |
| 全量清单核对 | 四个分片与 unittest 发现清单逐项一致，无遗漏或重复 |
| 新增专项 | 17 项，包括原生执行、严格条件、完整入参检查、权限、Plan、隔离派发、取消、保存失败、不重放、审批期间新输入、工具替换及跨进程目标暂停 |
| Python 3.9 专项 | 同一 17 项通过 |
| Node 桌面逻辑 | 72 项通过 |
| TypeScript / Vite | 通过；仍有既有大于 500 kB 的分包提示 |
| 源码隐藏 App 与正式 CLI | 本地协议服务下通过 |
| 更新后原 EXE：包内 Python | 74 项通过，四个包内 Python 子进程强制隔离导入 |
| 更新后原 EXE：隐藏 App 与正式 CLI | 通过，使用隔离配置与本地协议服务 |
| 最终代码冻结检查 | 441 个生产及测试文件在最终全量期间未变化 |

17 项中最后补入了工具在执行中被替换的绑定检查，因此重新运行最终全量；此前 926 项结果不作为最终版本的全量证明。115 项跳过保留原条件，不计为通过，也不作为跨平台能力证明。

App／CLI 端到端测试执行真实文件写入，验证条件不成立时后续文件不存在，过程显示编排及子工具图标，重开历史不会再次执行。测试检查 DOM 与交互结果，没有宣称截图式完整视觉审查。

本地模型夹具在工具已激活时，两步动作只需一次计划请求与一次最终回答请求；初次按需发现另有一次请求。执行两个动作之间未请求主模型。它证明执行链可省略该次往返，不能证明真实模型会自主选择、整体质量提高或固定比例降低费用。没有以夹具代替真实供应商长任务验收。

## 安装与复核

更新脚本状态为 `applied`，保留原启动路径 `desktop/release/Xuloom-win32-x64/Xuloom.exe` 及回滚资源。没有终止用户应用或生成新的功能版 EXE。EXE 本体 SHA256：

```text
2529b494f4123f152bce346051f7bd855ef95006424a561953cf17c49a3d1ba8
```

资源与当前源码逐文件比对：Python／Markdown 180 个、前端 62 个、Electron 18 个，全部一致。安装中只包含九个公开内置 Skill。

本地证据保存在忽略目录 `desktop/build`：`tool-sequence-contract-final-{0,1,2,3}.log`、`tool-sequence-final-coverage.json`、`tool-sequence-source-freeze.json`、`tool-sequence-contract-installed.json`、`tool-sequence-contract-installed-python.log`、`tool-sequence-contract-source-ui.log`、`tool-sequence-contract-installed-ui.log`、`tool-sequence-node.log` 和 `tool-sequence-build.log`。

本轮没有实施 CLI 启动依赖优化，没有引入额外 Ticket 系统、GitHub／飞书监听器或自动训练器。真实模型自主调用、物理跨机、原生 macOS／Linux 以及长期无人值守可靠性仍按原计划验收。
