# `except Exception` 审查（2026-11-11）

范围：`xuloom/` 下全部 96 处 `except Exception:`（另有约 100 处 `except Exception as error:`，它们会使用或上报错误，不在本轮范围内）。其中 54 处会静默继续（`pass`、`continue` 或返回空值/`False`/`None`），这 54 处逐一按“出错后会不会放行本应被拦截的操作、丢失数据或给出错误结论”判断。

## 已修复

| 位置 | 问题 | 处理 | 回归测试 |
| --- | --- | --- | --- |
| `core/hooks.py` `evaluate_before_tool_hooks` | `hooks.json` 无法解析，或某条 BeforeToolUse 规则在求值时出错，会被静默跳过，工具照常执行（fail-open）。 | 拒绝本次工具调用并给出原因，与 Python BeforeToolUse 回调的 `fail_closed` 一致。 | `tests/test_exception_audit.py` |
| `core/hooks.py` 其他事件（`dispatch`、`execute`、Stop、PostSampling、`has_configured`） | 配置或单条 Hook 出错时没有任何提示。 | 按消息记录一次 warning；`has_configured` 在配置损坏时返回 `True`，交给分发器报告。 | 同上 |
| `security/permissions.py` `_run_auto_classifier` | 安全分类器抛异常时被当作“没有异议”。 | 返回 `ask`，要求用户批准，并记录 warning。 | 同上 |
| `features/content_replacement.py` `_spill_and_preview` | 超大工具结果写盘失败后，仍用指向不存在文件的预览替换原文，原输出丢失；目录无法创建时还会因变量未赋值而崩溃。 | 写盘失败时保留原文、不替换，并记录 warning。 | 同上 |
| `utils/vcr.py` 严格回放 | 录像文件损坏或缺失时静默回落到真实模型请求。 | 在回放/mock 模型一项中处理，见 [回放与评测](../replay-and-eval.md)。 | `tests/test_replay_eval.py` |

## 保留（有意为之）

| 类别 | 例子 | 理由 |
| --- | --- | --- |
| 关闭/清理 | `engine.py` 关闭客户端与流、`model_stream.py` 关闭响应、`browser.py`、`lsp_client.py` terminate、`repl_tool.py` 删除临时文件、`scaling/control.py` | 发生在已经失败或正在关闭的路径上，原始错误才是要保留的结果。 |
| 删除会话附属文件 | `session.py` 清理子代理记录与计划文件 | 尽力清理；残留文件不影响正确性，可再次清理。 |
| 读取可选输入 | `context.py`、`memory_files.py`、`attachments.py` 读取 CLAUDE.md/附件，`file_state_cache.py` stat | 单个文件不可读时跳过该文件，其余上下文照常构建。 |
| 能力探测与估算 | `memory.py` tokenizer 回退、`auto_commit.py` 打开 Git 仓库、`sandbox.py` 读 HEAD | 回退到估算值或“不是 Git 仓库”，不会扩大权限。 |
| 保守默认值 | `tools.py` `is_concurrency_safe_call` 出错 → 串行执行；`plans.py` 路径比较出错 → 不是计划文件 | 出错时选择更严格的一侧。 |
| 显示/通知 | `compaction.py` 压缩完成日志、`tool_results.py` spill 记录回调、`cli_commands.py` 插件提示 | 检查点已经提交，显示失败不能改变结果。 |
| 后台重试 | `scaling/node.py` 持久化错误回写 | 下一轮循环会重试同一条记录。 |
| 记忆提取 | `extract_memories.py`、`session_memory.py` | 后台尽力任务；失败时记录 `last_error`，不影响主任务。 |

未在本轮修改的“保留”项建议后续逐步收窄异常类型（例如 `OSError`），但不改变行为。
