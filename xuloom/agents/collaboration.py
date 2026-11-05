"""Delegation policy and counters over the existing native worker lifecycle."""
import uuid
from contextvars import ContextVar

DELEGATION_BUDGET = ContextVar("xuloom_delegation_budget", default=None)
MODES = ("solo", "auto", "ultra")
# Mode is a delegation preference; limits belong to the host, independently.
DEFAULTS = {
    "solo": {"concurrency": 0, "max_delegations": 0, "output_tokens": 0, "request_output_tokens": 0},
    "auto": {"concurrency": None, "max_delegations": None, "output_tokens": None, "request_output_tokens": 64000},
    "ultra": {"concurrency": None, "max_delegations": None, "output_tokens": None, "request_output_tokens": 64000},
}


def policy(mode="auto", settings=None):
    if mode not in MODES:
        raise ValueError("Collaboration mode must be solo, auto or ultra")
    settings = settings or {}
    values = dict(DEFAULTS[mode])
    if mode != "solo":
        if "agent_concurrency" in settings:
            values["concurrency"] = settings["agent_concurrency"]
        limits = settings.get("agent_limits", {})
        if not isinstance(limits, dict) or limits.keys() - values.keys():
            raise ValueError("Invalid agent limits")
        values.update(limits)
        for name, value in values.items():
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError("Invalid agent limit: " + name)
        if values["request_output_tokens"] is None:
            raise ValueError("A finite per-response model limit is required")
    return {"mode": mode, **values}


def new_run(profile):
    return {"id": str(uuid.uuid4()), "submitted": 0, "budget": {"output_tokens": profile["output_tokens"]},
            "output_used": 0, "output_reserved": 0,
            "request_cap": profile["request_output_tokens"]}


def admit(context, count):
    profile = context.get("collaboration") or policy()
    require_delegation(context)
    run = context.setdefault("collaboration_run", new_run(profile))
    if profile["max_delegations"] is not None and run["submitted"] + count > profile["max_delegations"]:
        raise ValueError("本轮累计子任务已达协作预算上限；请汇总已有结果，不要继续派发。")
    if run["budget"]["output_tokens"] is not None and run["output_used"] + run["output_reserved"] >= run["budget"]["output_tokens"]:
        raise ValueError("本轮子代理输出预算已耗尽；请汇总已有结果。")
    # Atomic on the owning event loop; count admission attempts conservatively
    # even if registration/persistence subsequently fails.
    run["submitted"] += count


def require_delegation(context):
    if (context.get("collaboration") or policy())["mode"] == "solo":
        raise ValueError("Solo 独立模式禁止委派。请用户切换到 Auto 或 Ultra；完全访问不会解除此限制。")


def instructions(profile):
    mode = profile["mode"]
    principle = {
        "solo": "Complete work yourself. Do not spawn or resume subagents. If the user explicitly requests parallel workers, explain Solo is selected and ask them to switch the collaboration menu; do not switch it yourself.",
        "auto": "Delegate clearly independent work when benefits exceed coordination cost; answer simple tasks directly.",
        "ultra": "Proactively delegate independent research, alternatives or checks early; no extra request is needed. Keep the critical path and synthesis yourself and work alongside agents. Answer simple tasks directly.",
    }[mode]
    return (f"# Collaboration: {mode}\n{principle}\n"
            "Give self-contained assignments and avoid duplicate work. "
            "Runtime queues workers FIFO; no manual concurrency batching. Auto/Ultra change initiative, not quantities or permissions.")
