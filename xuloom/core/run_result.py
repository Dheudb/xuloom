"""Execution receipts describe harness termination, not domain-level acceptance."""
from dataclasses import asdict, dataclass, replace


class PersistenceError(OSError):
    """Execution cannot safely continue without its durable checkpoint."""


def failure_help(reason):
    """A display projection of existing receipts, not another execution state."""
    rows = {
        "not_configured": ("configuration", "请在设置中填写模型连接，再继续当前会话。", "settings"),
        "api_error": ("model_service", "检查模型、接口协议、Key、代理及服务额度；已有记录保留。", "settings"),
        "tool_input_error": ("tool_input", "查看被拒绝的工具参数；已完成操作保留，修正后继续。", "activity"),
        "tool_protocol_error": ("tool_input", "模型多次返回无效调用；查看 Retry 与工具详情，检查模型工具调用能力。", "activity"),
        "approval_required": ("permission", "操作尚未获得授权；回到交互会话处理权限请求。", "activity"),
        "input_required": ("input", "任务需要你的回答；回到交互会话处理问题。", "activity"),
        "persistence_error": ("storage", "检查磁盘空间和目录权限；操作可能已生效，核查已有结果后再继续。", "activity"),
        "unsupported_input": ("capability", "检查模型支持的图片、文档等输入类型。", "settings"),
        "session_busy": ("busy", "另一个任务或进程正在使用此会话；等待其结束或正常停止。", "activity"),
        "session_conflict": ("storage", "重新加载最新记录；不要用旧检查点覆盖或重放操作。", "activity"),
        "cancelled": ("cancelled", "执行已停止；已保存结果保留，未确认副作用不会自动重放。", "activity"),
        "blocked": ("blocked", "查看阻塞原因与待处理要求，再决定继续或调整任务。", "activity"),
        "cost_limit": ("budget", "已达到任务花费上限，或所用模型没有配置价格；调整上限或价格后再继续。", "settings"),
        "budget_exhausted": ("budget", "已达到已设置的执行预算；已有结果保留，调整对应任务或委派预算后再继续。", "activity"),
        "workflow_incomplete": ("blocked", "流程尚未完成；查看已有回执与未完成步骤后继续。", "activity"),
    }
    category, message, action = rows.get(reason, ("execution", "查看活动和已有回执；确认当前状态后再继续。", "activity"))
    return {"category": category, "message": message, "action": action}


@dataclass(frozen=True)
class RunResult:
    text: str
    reason: str
    persisted: bool = False
    execution_reason: str = ""
    session_id: str = ""
    agent_id: str = ""
    user_message_id: str = ""

    @property
    def status(self):
        if self.reason == "completed" and self.persisted:
            return "completed"
        if self.reason == "cancelled":
            return "cancelled"
        return "crashed"

    @property
    def ok(self):
        return self.reason == "completed" and self.persisted

    def to_dict(self):
        return {**asdict(self), "status": self.status, "ok": self.ok}

    def __str__(self):
        return self.text


def finish_run(engine, text, reason):
    from xuloom.core.tool_execution import tool_identity
    user_message_id, _ = tool_identity(engine, "", {})
    owner = engine.tool_context.get("delegation", {}).get("owner_session_id")
    result = RunResult(str(text), reason, persisted=True, execution_reason=reason,
        session_id=owner or engine.session_id, agent_id=engine.agent_id or "", user_message_id=user_message_id)
    engine.last_run_result = result.to_dict()
    try:
        saved = engine.persist_session_state()
    except Exception:
        saved = False
    if not saved:
        result = replace(result, reason="persistence_error", persisted=False)
        engine.last_run_result = result.to_dict()
    return result
